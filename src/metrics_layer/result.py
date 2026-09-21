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


NET_DEBT_UNRELIABLE = "net_debt_unreliable"
DEBT_DATA_COMPLETELY_MISSING = "debt_data_completely_missing"


def net_debt_none_reason(period):
    """invalid_reason for a period whose net_debt is None.

    net_debt=None stays the single sentinel (docs/decisions.md, "SimFin-
    Client" Entscheidung 10) - the adapter distinguishes *why* via
    data_quality_flags. "debt_data_completely_missing" if the period carries
    that flag, else "net_debt_unreliable". A period without a
    data_quality_flags key (e.g. the dummy fixtures) is always the latter.
    """
    # `or ()`, not just a .get default: a key present but null (e.g. read
    # back from JSON/DB) must behave like an absent one, not raise TypeError.
    if DEBT_DATA_COMPLETELY_MISSING in (period.get("data_quality_flags") or ()):
        return DEBT_DATA_COMPLETELY_MISSING
    return NET_DEBT_UNRELIABLE


class MetricInputError(Exception):
    """Raised when a Stage-1 metric function is called with malformed input.

    Reserved for caller errors (e.g. duplicate/un-deduplicated filings for
    the same fiscal year), never for a valid-but-unfavorable data property
    of a company - those are MetricResult.invalid_reason instead.
    """
