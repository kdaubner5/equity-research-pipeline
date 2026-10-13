"""Kandidaten-Ticker aus den Kandidatenlisten (docs/kandidaten/runde*.md) lesen.

Die Liste in Markdown ist die einzige Quelle: Wer dort in der Tabelle steht, bekommt Kurse. Eine neue Runde (neue
Datei runde2.md usw.) wird ohne Codeaenderung uebernommen. Gelesen werden nur Tabellenzeilen, deren erste Spalte eine
Zahl ist (laufende Nummer); die dritte Spalte ist der Ticker. Text ausserhalb der Tabelle (z. B. "Gestrichen: XPER")
zaehlt nicht.
"""

import re
from pathlib import Path
from typing import List

_TICKER = re.compile(r"^[A-Z][A-Z0-9]{0,5}([.-][A-Z0-9]{1,3})?$")


class CandidateListError(Exception):
    """Eine Kandidatenliste fehlt, ist leer oder enthaelt einen unbrauchbaren Ticker; die Meldung nennt Datei und Zeile."""


def read_candidate_tickers(path) -> List[str]:
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CandidateListError(f"cannot read {path}: {exc}") from exc
    tickers: List[str] = []
    header_ok = False
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells[0].isdigit():
            if len(cells) >= 3 and cells[2] == "Ticker":
                header_ok = True
            continue
        if len(cells) < 3:
            raise CandidateListError(f"{path.name} line {number}: row has fewer than 3 columns")
        if not header_ok:
            raise CandidateListError(f"{path.name} line {number}: no table header with 'Ticker' as 3rd column before the rows")
        ticker = cells[2]
        if not _TICKER.match(ticker):
            raise CandidateListError(f"{path.name} line {number}: unusable ticker {ticker!r}")
        if ticker in tickers:
            raise CandidateListError(f"{path.name} line {number}: ticker {ticker!r} is listed twice")
        tickers.append(ticker)
    if not tickers:
        raise CandidateListError(f"{path.name} contains no candidate rows (expected a table with number and ticker)")
    return tickers


def collect_candidate_tickers(repo_root, pattern) -> List[str]:
    """Alle Ticker aller Dateien, die auf `pattern` (relativ zum Repo) passen; ein Ticker in mehreren Runden zaehlt einmal."""
    root = Path(repo_root)
    files = sorted(root.glob(pattern))
    if not files:
        raise CandidateListError(f"no candidate list matches {pattern!r} under {root}")
    result: List[str] = []
    for file in files:
        for ticker in read_candidate_tickers(file):
            if ticker not in result:
                result.append(ticker)
    return result
