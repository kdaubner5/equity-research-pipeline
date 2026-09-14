from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MetricResult:
    """Result of a Stage-1 metric calculation.

    value is None whenever invalid_reason is set, and vice versa. Binding
    convention for all Stage-1 metrics (ROIC, Net Debt/EBITDA, Cash
    Conversion) per docs/decisions.md - never return a bare None.
    """

    value: Optional[float]
    invalid_reason: Optional[str] = None
