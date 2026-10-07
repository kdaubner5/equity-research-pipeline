"""Peer percentile and peer-group selection for Stage 2 (docs/decisions.md "Stufe 2:
Peer-Gruppen und relative Bewertung", Abschnitt 2).

Pure functions over already-computed valid EV/EBITDA values: no I/O, no thresholds of
their own (min_size and the coarse divisor come from config/stage2.yaml via
PeerGroupsConfig). Percentiles are exact Fractions, so "rank 4 of 10" is exactly 1/3 and a
boundary comparison never depends on floating-point rounding.

Level choice: fine (same sectorCode) if it holds at least min_size valid values - the
evaluated company included - else coarse (sectorCode // coarse_level_divisor) if that does,
else absolute (no peer comparison). A group below min_size is never used.
"""

from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping, Optional, Sequence, Tuple

from config_layer.stage2 import PeerGroupsConfig

LEVEL_FINE = "fine"
LEVEL_COARSE = "coarse"
LEVEL_ABSOLUTE = "absolute"


@dataclass(frozen=True)
class PeerPosition:
    rank: int  # competition rank, 1 = cheapest (1 + number of strictly cheaper values)
    n: int
    percentile: Fraction  # 0 = cheapest, 1 = dearest; mid-rank for ties: (mid_rank - 1)/(n - 1)


@dataclass(frozen=True)
class PeerAssignment:
    """The peer group a company is compared in. For level "absolute" group_key, rank, n and
    percentile are None; n_fine and n_coarse (valid values incl. the company) are always set,
    so the reason for a fallback stays visible."""

    level: str
    group_key: Optional[int]
    rank: Optional[int]
    n: Optional[int]
    percentile: Optional[Fraction]
    n_fine: int
    n_coarse: int


def peer_position(value, group_values: Sequence[float]) -> PeerPosition:
    """Rank and percentile of value within group_values (which must contain it)."""
    n = len(group_values)
    if n < 2:
        raise ValueError(f"a percentile needs at least 2 values, got {n}")
    below = sum(1 for v in group_values if v < value)
    equal = sum(1 for v in group_values if v == value)
    if equal == 0:
        raise ValueError(f"value {value!r} is not among the group values")
    mid_rank = Fraction(2 * below + equal + 1, 2)
    return PeerPosition(rank=below + 1, n=n, percentile=(mid_rank - 1) / (n - 1))


def assign_peers(
    ticker: str,
    sector_code: int,
    ev_ebitda: float,
    base: Mapping[str, Tuple[int, float]],
    groups: PeerGroupsConfig,
) -> PeerAssignment:
    """Choose the peer level for one company and place it within the chosen group.

    base maps every valid peer-base member's ticker to (sector_code, ev_ebitda); the
    evaluated company must be one of them with exactly these values (anything else is a
    caller error, not company data).
    """
    if ticker not in base:
        raise ValueError(f"{ticker!r} is not in the peer base")
    if base[ticker] != (sector_code, ev_ebitda):
        raise ValueError(
            f"{ticker!r}: sector/multiple {(sector_code, ev_ebitda)!r} differ from the peer base entry {base[ticker]!r}"
        )

    divisor = groups.coarse_level_divisor
    coarse_key = sector_code // divisor
    fine_values = [value for sector, value in base.values() if sector == sector_code]
    coarse_values = [value for sector, value in base.values() if sector // divisor == coarse_key]
    n_fine, n_coarse = len(fine_values), len(coarse_values)

    if n_fine >= groups.min_size:
        level, group_key, values = LEVEL_FINE, sector_code, fine_values
    elif n_coarse >= groups.min_size:
        level, group_key, values = LEVEL_COARSE, coarse_key, coarse_values
    else:
        return PeerAssignment(LEVEL_ABSOLUTE, None, None, None, None, n_fine, n_coarse)

    position = peer_position(ev_ebitda, values)
    return PeerAssignment(level, group_key, position.rank, position.n, position.percentile, n_fine, n_coarse)
