"""data/benchmark_prices.db: gespeicherte Schlusskurse der Messlatten-ETFs (docs/decisions.md, "Messlatte -
Kursreihen speichern und regelmaessig abrufen", Beschluss von Konstantin am 09.10.2026).

Eigene Datei, nicht das Run-Archiv. Regeln:
- Der juengste Kurs eines Abrufs ist *vorlaeufig* und wird beim naechsten Abruf ersetzt (ohne Konflikt). Aeltere
  Kurse sind endgueltig und werden nie ueberschrieben; ein abweichender neuer Wert landet in
  benchmark_price_conflicts und wird gemeldet (Konstantin entscheidet von Hand).
- Ein nicht positiver oder nicht numerischer Kurs wird nicht gespeichert, sondern gemeldet.
- Ein Tagessprung ueber der Schwelle wird gespeichert, markiert (flag = 'jump') und gemeldet.
- Luecken (am Rand zwischen zwei Abrufen und innerhalb eines Abrufs) werden gemeldet; Schwelle in Kalendertagen aus
  der Konfiguration, ohne Boersenkalender.
- Je Symbol laeuft das Speichern in einer Transaktion (alles oder nichts).
"""

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

STATUS_OK = "ok"


class BenchmarkStoreError(Exception):
    """Ungueltige Eingabe fuer den Kursspeicher (zum Beispiel ein Symbol, das nicht in der Konfiguration steht)."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmark_prices (
    symbol TEXT NOT NULL,
    price_date TEXT NOT NULL,
    close REAL NOT NULL CHECK (close > 0),
    currency TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL,
    provisional INTEGER NOT NULL CHECK (provisional IN (0, 1)),
    flag TEXT,
    PRIMARY KEY (symbol, price_date)
);
CREATE TABLE IF NOT EXISTS benchmark_fetches (
    fetch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    rows_returned INTEGER,
    first_date TEXT,
    last_date TEXT,
    status TEXT NOT NULL,
    message TEXT
);
CREATE TABLE IF NOT EXISTS benchmark_price_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    price_date TEXT NOT NULL,
    stored_close REAL NOT NULL,
    new_close REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (symbol, price_date, stored_close, new_close)
);
"""


@dataclass
class FetchReport:
    symbol: str
    inserted: int = 0
    unchanged: int = 0
    replaced_provisional: int = 0
    conflicts: List[Tuple[str, float, float]] = field(default_factory=list)  # (Datum, gespeichert, neu)
    rejected: List[str] = field(default_factory=list)
    jumps: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)

    def warnings(self) -> List[str]:
        lines = []
        for day, stored, new in self.conflicts:
            lines.append(f"{self.symbol}: Konflikt am {day}: gespeichert {stored}, neu geliefert {new} (nicht überschrieben)")
        for day in self.rejected:
            lines.append(f"{self.symbol}: Kurs am {day} abgelehnt (nicht positiv, keine Zahl oder ungültiges Datum)")
        for day in self.jumps:
            lines.append(f"{self.symbol}: Sprung über der Schwelle am {day} (gespeichert, markiert)")
        for gap in self.gaps:
            lines.append(f"{self.symbol}: Lücke: {gap}")
        return lines


def connect(path) -> sqlite3.Connection:
    """Oeffnet (und legt bei Bedarf an) die Kursdatei; der Ordner wird angelegt, falls er fehlt."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA)
    connection.commit()
    return connection


def _parse_price(raw) -> Optional[float]:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _parse_date(raw) -> Optional[date]:
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _insert_price(connection, symbol, day, close, currency, fetched_at, source, provisional, flag):
    connection.execute(
        "INSERT INTO benchmark_prices (symbol, price_date, close, currency, fetched_at, source, provisional, flag) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, day, close, currency, fetched_at, source, 1 if provisional else 0, flag),
    )


def store_fetch(connection, config, symbol, rows: Dict[str, object], fetched_at) -> FetchReport:
    """Speichert einen Abruf (Datum -> Schlusskurs) fuer ein Symbol in einer Transaktion und meldet Auffaelligkeiten."""
    entry = config.symbol(symbol)
    if entry is None:
        raise BenchmarkStoreError(f"symbol {symbol!r} is not in config/benchmarks.yaml")

    report = FetchReport(symbol=symbol)
    valid: Dict[str, float] = {}
    for raw_day, raw_close in rows.items():
        close = _parse_price(raw_close)
        if close is None or _parse_date(raw_day) is None:
            report.rejected.append(str(raw_day))
            continue
        valid[str(raw_day)] = close
    days = sorted(valid)

    with connection:  # eine Transaktion je Symbol: alles oder nichts
        last_stored = connection.execute(
            "SELECT MAX(price_date) FROM benchmark_prices WHERE symbol = ?", (symbol,)
        ).fetchone()[0]

        # Luecken: am Rand (neuer Abruf beginnt weit nach dem letzten gespeicherten Kurs) und innerhalb des Abrufs.
        gap_limit = timedelta(days=config.gap_calendar_days)
        if days and last_stored is not None:
            previous, first = date.fromisoformat(last_stored), date.fromisoformat(days[0])
            if first > previous and first - previous > gap_limit:
                report.gaps.append(f"zwischen {last_stored} und {days[0]} fehlen Kurse (zu lange nicht abgerufen)")
        for before, after in zip(days, days[1:]):
            if date.fromisoformat(after) - date.fromisoformat(before) > gap_limit:
                report.gaps.append(f"im Abruf zwischen {before} und {after}")

        jump_days = set()
        for before, after in zip(days, days[1:]):
            change = abs(valid[after] / valid[before] - 1) * 100
            if round(change, 9) > config.jump_threshold_pct:  # Rundung gegen Gleitkomma-Rauschen an der Grenze
                jump_days.add(after)
                report.jumps.append(after)

        newest = days[-1] if days else None
        for day in days:
            close = valid[day]
            provisional = day == newest
            flag = "jump" if day in jump_days else None
            existing = connection.execute(
                "SELECT close, provisional FROM benchmark_prices WHERE symbol = ? AND price_date = ?", (symbol, day)
            ).fetchone()
            if existing is None:
                _insert_price(connection, symbol, day, close, entry.currency, fetched_at, config.source, provisional, flag)
                report.inserted += 1
                continue
            stored_close, stored_provisional = existing
            if stored_provisional:
                if not math.isclose(stored_close, close, rel_tol=0, abs_tol=1e-9):
                    report.replaced_provisional += 1
                else:
                    report.unchanged += 1
                connection.execute(
                    "UPDATE benchmark_prices SET close = ?, fetched_at = ?, provisional = ?, flag = ? "
                    "WHERE symbol = ? AND price_date = ?",
                    (close, fetched_at, 1 if provisional else 0, flag, symbol, day),
                )
            elif math.isclose(stored_close, close, rel_tol=0, abs_tol=1e-9):
                report.unchanged += 1
            else:
                report.conflicts.append((day, stored_close, close))
                connection.execute(
                    "INSERT OR IGNORE INTO benchmark_price_conflicts "
                    "(symbol, price_date, stored_close, new_close, fetched_at) VALUES (?, ?, ?, ?, ?)",
                    (symbol, day, stored_close, close, fetched_at),
                )

        connection.execute(
            "INSERT INTO benchmark_fetches (symbol, fetched_at, rows_returned, first_date, last_date, status, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (symbol, fetched_at, len(rows), days[0] if days else None, days[-1] if days else None, STATUS_OK, None),
        )
    return report


def record_failed_fetch(connection, symbol, fetched_at, status, message) -> None:
    """Protokolliert einen gescheiterten Abruf (Meldung ohne Schluessel); es werden keine Kurse gespeichert."""
    with connection:
        connection.execute(
            "INSERT INTO benchmark_fetches (symbol, fetched_at, rows_returned, first_date, last_date, status, message) "
            "VALUES (?, ?, NULL, NULL, NULL, ?, ?)",
            (symbol, fetched_at, status, message),
        )


def read_close(connection, config, symbol, on_date: date) -> Optional[Tuple[str, float]]:
    """(Kursdatum, Schlusskurs) am Tag oder, an Wochenenden und Feiertagen, der letzte gespeicherte Kurs davor - hoechstens
    lookback_max_calendar_days zurueck. Sonst None ("nicht auswertbar"), nie ein stiller Ersatzwert."""
    row = connection.execute(
        "SELECT price_date, close FROM benchmark_prices WHERE symbol = ? AND price_date <= ? "
        "ORDER BY price_date DESC LIMIT 1",
        (symbol, on_date.isoformat()),
    ).fetchone()
    if row is None:
        return None
    if on_date - date.fromisoformat(row[0]) > timedelta(days=config.lookback_max_calendar_days):
        return None
    return row[0], row[1]


def stale_warning(connection, config, symbol, today: date) -> Optional[str]:
    """Warntext, wenn der letzte gespeicherte Kurs aelter ist als stale_warn_days (oder es gar keinen gibt)."""
    last = connection.execute("SELECT MAX(price_date) FROM benchmark_prices WHERE symbol = ?", (symbol,)).fetchone()[0]
    if last is None:
        return f"{symbol}: noch kein Kurs gespeichert"
    age = (today - date.fromisoformat(last)).days
    if age > config.stale_warn_days:
        return f"{symbol}: letzter gespeicherter Kurs vom {last} ist {age} Tage älter als heute (Schwelle {config.stale_warn_days})"
    return None
