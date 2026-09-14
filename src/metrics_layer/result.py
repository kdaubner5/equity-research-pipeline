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

    def __post_init__(self):
        if (self.value is None) == (self.invalid_reason is None):
            raise ValueError(
                "MetricResult requires exactly one of value or invalid_reason to be set, "
                f"got value={self.value!r} invalid_reason={self.invalid_reason!r}"
            )


class MetricInputError(Exception):
    """Raised when a Stage-1 metric function is called with malformed input.

    Reserved for caller errors (e.g. duplicate/un-deduplicated filings for
    the same fiscal year), never for a valid-but-unfavorable data property
    of a company - those are MetricResult.invalid_reason instead.
    """
