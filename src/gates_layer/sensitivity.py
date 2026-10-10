"""Empfindlichkeitstest der Stufe-1-Schwellen auf gespeicherten Kennzahlen (Evidenz-Check der Filterstruktur).

Frage: Wie aendert sich das Urteil je Firma, wenn eine Schwelle strenger oder lockerer waere? Gerechnet wird aus den im
Archiv gespeicherten Rohwerten (ROIC, Net Debt/EBITDA, Cash Conversion, Nettomarge) mit derselben Funktion wie im Lauf
(evaluate_stage1_gates). Pro Variante aendert sich genau eine Schwelle (oder beide Schwellen einer Kennzahl gemeinsam).

Grenzen, die das Ergebnis immer begleiten:
- Nur die Gate-Schwellen werden variiert, nicht die Rechenparameter der Kennzahlen (Steuersatz, Obergrenze fuer den ROIC),
  denn dafuer muessten die Kennzahlen aus den Fundamentaldaten neu berechnet werden.
- Fuer Ueberlebende ist die Nettomarge nicht gespeichert; sie bestanden die Grenze, eine strengere Marge laesst sich
  fuer sie nicht beurteilen (solche Varianten werden uebersprungen).
- Eine Variante, die die Reihenfolge der Schwellen verletzt (zum Beispiel eine saubere Grenze unter der Durchfall-Grenze),
  wird uebersprungen und benannt.
- Der Test sagt, welche Zahlen die Auswahl steuern, nicht, welche richtig sind.
"""

import dataclasses
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from config_layer.thresholds import Thresholds, ThresholdsConfigError
from gates_layer.stage1_gates import (
    FAILED,
    NOT_EVALUABLE,
    PASSED,
    PASSED_WITH_FLAGS,
    UNSUPPORTED_BUSINESS_MODEL,
    evaluate_stage1_gates,
)
from metrics_layer.result import MetricResult

STATUSES = (PASSED, PASSED_WITH_FLAGS, FAILED, NOT_EVALUABLE)
SURVIVOR_STATUSES = (PASSED, PASSED_WITH_FLAGS)
_NO_VALUE_REASON = "value_not_stored"
# Nettomarge der Ueberlebenden: nicht gespeichert; sie haben die Grenze bestanden. Ein sehr hoher Platzhalterwert laesst
# jede lockerere Variante unveraendert; strengere Marge-Varianten werden deshalb ganz uebersprungen.
_SURVIVOR_MARGIN = 1.0

# (Kurzname, Schwellengruppe, Felder)
PARAMETERS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("roic.clean_pass_above", "roic", ("clean_pass_above",)),
    ("roic.fail_below", "roic", ("fail_below",)),
    ("roic.beide", "roic", ("clean_pass_above", "fail_below")),
    ("net_debt_ebitda.clean_pass_below", "net_debt_ebitda", ("clean_pass_below",)),
    ("net_debt_ebitda.fail_at_or_above", "net_debt_ebitda", ("fail_at_or_above",)),
    ("net_debt_ebitda.beide", "net_debt_ebitda", ("clean_pass_below", "fail_at_or_above")),
    ("cash_conversion.clean_pass_at_or_above", "cash_conversion", ("clean_pass_at_or_above",)),
    ("cash_conversion.fail_below", "cash_conversion", ("fail_below",)),
    ("cash_conversion.beide", "cash_conversion", ("clean_pass_at_or_above", "fail_below")),
    ("net_income_margin.fail_below", "net_income_margin", ("fail_below",)),
)


class SensitivityError(Exception):
    """Der Test kann nicht laufen (zum Beispiel: der Lauf ist im Archiv nicht vorhanden)."""


@dataclass(frozen=True)
class Stage1Row:
    ticker: str
    stored_status: str
    roic: Optional[float]
    net_debt_ebitda: Optional[float]
    cash_conversion: Optional[float]
    net_income_margin: Optional[float]  # None: nicht gespeichert (Ueberlebende) oder nicht berechnet
    is_survivor: bool


@dataclass
class Cell:
    parameter: str
    step_percent: float
    counts: Dict[str, int] = field(default_factory=dict)
    changed: Dict[str, Tuple[str, str]] = field(default_factory=dict)  # Ticker -> (Basis, Variante)
    skipped: Optional[str] = None

    @property
    def selection_in(self) -> Dict[str, Tuple[str, str]]:
        """Firmen, die durch die Variante neu zu den Ueberlebenden von Stufe 1 gehoeren."""
        return {t: c for t, c in self.changed.items() if c[0] not in SURVIVOR_STATUSES and c[1] in SURVIVOR_STATUSES}

    @property
    def selection_out(self) -> Dict[str, Tuple[str, str]]:
        """Firmen, die durch die Variante aus den Ueberlebenden herausfallen."""
        return {t: c for t, c in self.changed.items() if c[0] in SURVIVOR_STATUSES and c[1] not in SURVIVOR_STATUSES}

    @property
    def rank_only(self) -> Dict[str, Tuple[str, str]]:
        """Wechsel innerhalb der Ueberlebenden (bestanden <-> bestanden mit Marken): aendert nur den Rang."""
        return {t: c for t, c in self.changed.items() if c[0] in SURVIVOR_STATUSES and c[1] in SURVIVOR_STATUSES}

    @property
    def selection_changes(self) -> int:
        return len(self.selection_in) + len(self.selection_out)


@dataclass
class SensitivityResult:
    baseline_counts: Dict[str, int]
    baseline_mismatches: List[Tuple[str, str, str]]  # (Ticker, gespeichert, neu gerechnet)
    cells: List[Cell]
    companies: int
    set_aside: int

    def cell(self, parameter: str, step_percent: float) -> Cell:
        for entry in self.cells:
            if entry.parameter == parameter and entry.step_percent == step_percent:
                return entry
        raise KeyError((parameter, step_percent))


def load_stage1_rows(connection, run_timestamp: str) -> List[Stage1Row]:
    """Alle Firmen eines Stufe-1-Laufs aus run_results (Ueberlebende) und eliminated_results (Rest)."""
    available = [row[0] for row in connection.execute("SELECT run_timestamp FROM runs ORDER BY run_timestamp")]
    if run_timestamp not in available:
        raise SensitivityError(f"Lauf {run_timestamp} nicht im Archiv; vorhanden: {available}")
    rows = []
    for ticker, status, roic, nde, cc in connection.execute(
        "SELECT ticker, status, roic_3yr, net_debt_ebitda, cash_conversion_3yr FROM run_results WHERE run_timestamp = ?",
        (run_timestamp,),
    ):
        rows.append(Stage1Row(ticker, status, roic, nde, cc, None, True))
    for ticker, status, roic, nde, cc, margin in connection.execute(
        "SELECT ticker, status, roic, net_debt_ebitda, cash_conversion, net_income_margin FROM eliminated_results "
        "WHERE run_timestamp = ?",
        (run_timestamp,),
    ):
        rows.append(Stage1Row(ticker, status, roic, nde, cc, margin, False))
    tickers = [r.ticker for r in rows]
    duplicates = sorted({x for x in tickers if tickers.count(x) > 1})
    if duplicates:
        raise SensitivityError(f"Ticker mehrfach im Lauf {run_timestamp} (run_results und eliminated_results): {duplicates[:10]}")
    return rows


def available_run_timestamps(connection) -> List[str]:
    return [row[0] for row in connection.execute("SELECT run_timestamp FROM runs ORDER BY run_timestamp")]


def _metric(value):
    if value is None:
        return MetricResult(value=None, invalid_reason=_NO_VALUE_REASON)
    return MetricResult(value=float(value))


def reclassify(row: Stage1Row, thresholds: Thresholds) -> str:
    margin = _SURVIVOR_MARGIN if (row.is_survivor and row.net_income_margin is None) else row.net_income_margin
    result = evaluate_stage1_gates(
        _metric(row.roic),
        _metric(row.net_debt_ebitda),
        _metric(row.cash_conversion),
        _metric(margin),
        thresholds=thresholds,
    )
    return result.status


def _counts(statuses: Sequence[str]) -> Dict[str, int]:
    return {name: sum(1 for s in statuses if s == name) for name in STATUSES}


def _scaled(thresholds: Thresholds, group: str, fields: Tuple[str, ...], step_percent: float) -> Thresholds:
    section = getattr(thresholds, group)
    # Rundung gegen Gleitkomma-Rauschen an den Grenzen (0.10 * 1.1 ergibt sonst 0.11000000000000001)
    changes = {name: round(getattr(section, name) * (1 + step_percent / 100.0), 10) for name in fields}
    return dataclasses.replace(thresholds, **{group: dataclasses.replace(section, **changes)})


def run_sensitivity(rows: Sequence[Stage1Row], thresholds: Thresholds, steps: Sequence[float]) -> SensitivityResult:
    evaluated = [r for r in rows if r.stored_status != UNSUPPORTED_BUSINESS_MODEL]
    set_aside = len(rows) - len(evaluated)

    baseline = {r.ticker: reclassify(r, thresholds) for r in evaluated}
    mismatches = [(r.ticker, r.stored_status, baseline[r.ticker]) for r in evaluated if baseline[r.ticker] != r.stored_status]

    cells = []
    for name, group, fields in PARAMETERS:
        for step in steps:
            cell = Cell(parameter=name, step_percent=step)
            if group == "net_income_margin" and step > 0:
                cell.skipped = (
                    "strengere Marge nicht beurteilbar: für Überlebende ist die Nettomarge nicht gespeichert"
                )
                cells.append(cell)
                continue
            try:
                variant = _scaled(thresholds, group, fields, step)
            except (ThresholdsConfigError, ValueError) as exc:
                cell.skipped = f"ungültige Kombination, Reihenfolge der Schwellen verletzt ({exc})"
                cells.append(cell)
                continue
            new = {r.ticker: reclassify(r, variant) for r in evaluated}
            cell.counts = _counts(list(new.values()))
            cell.changed = {t: (baseline[t], new[t]) for t in new if new[t] != baseline[t]}
            cells.append(cell)

    return SensitivityResult(
        baseline_counts=_counts(list(baseline.values())),
        baseline_mismatches=mismatches,
        cells=cells,
        companies=len(evaluated),
        set_aside=set_aside,
    )
