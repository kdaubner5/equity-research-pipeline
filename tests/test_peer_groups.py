"""Peer percentile and peer-group selection (docs/decisions.md "Stufe 2: Peer-Gruppen und
relative Bewertung", Abschnitte 2 und 6, Test 2). Artificial multiples only."""

import random
from fractions import Fraction

import pytest

from gates_layer.peer_groups import (
    LEVEL_ABSOLUTE,
    LEVEL_COARSE,
    LEVEL_FINE,
    assign_peers,
    peer_position,
)
from stage2_helpers import make_config

CONFIG = make_config()
GROUPS = CONFIG.peer_groups


def _base(sector_code, n, *, start=0, prefix=None):
    """n base members of one sector with multiples 5.0, 6.0, ... (ticker unique per sector)."""
    prefix = prefix or f"S{sector_code}_"
    return {f"{prefix}{i}": (sector_code, 5.0 + i) for i in range(start, start + n)}


# ---------------------------------------------------------------- peer_position


def test_percentile_for_eight_distinct_values():
    values = [5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0]
    percentiles = [peer_position(v, values).percentile for v in values]
    assert percentiles == [Fraction(i, 7) for i in range(8)]
    assert [peer_position(v, values).rank for v in values] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert all(peer_position(v, values).n == 8 for v in values)


def test_percentile_uses_the_mid_rank_for_ties():
    values = [5.0, 7.0, 7.0, 9.0]
    position = peer_position(7.0, values)
    assert position.rank == 2  # competition rank: one value is strictly cheaper
    assert position.percentile == Fraction(3, 2) / 3  # mid rank 2.5 -> (2.5 - 1)/(4 - 1) = 1/2


def test_percentile_of_all_equal_values_is_one_half():
    assert peer_position(7.0, [7.0] * 5).percentile == Fraction(1, 2)


def test_percentile_for_n_equal_two():
    assert peer_position(1.0, [1.0, 2.0]).percentile == Fraction(0)
    assert peer_position(2.0, [1.0, 2.0]).percentile == Fraction(1)


def test_percentile_is_exact_rank_four_of_ten_is_exactly_one_third():
    values = [float(i) for i in range(1, 11)]
    assert peer_position(4.0, values).percentile == Fraction(1, 3)


def test_percentile_needs_the_value_in_the_group_and_two_members():
    with pytest.raises(ValueError):
        peer_position(3.0, [1.0, 2.0])
    with pytest.raises(ValueError):
        peer_position(1.0, [1.0])


def test_percentile_is_independent_of_the_order_of_the_group_values():
    values = [9.0, 5.0, 12.0, 7.0, 7.0, 6.0, 11.0, 8.0]
    shuffled = values[:]
    random.Random(1).shuffle(shuffled)
    for v in values:
        assert peer_position(v, values) == peer_position(v, shuffled)


# ---------------------------------------------------------------- assign_peers: level choice


def test_fine_level_with_exactly_the_minimum_size():
    base = _base(107002, 8)
    assignment = assign_peers("S107002_3", 107002, 8.0, base, GROUPS)
    assert assignment.level == LEVEL_FINE
    assert assignment.group_key == 107002
    assert assignment.n == 8
    assert assignment.n_fine == 8
    assert assignment.n_coarse == 8
    assert assignment.rank == 4
    assert assignment.percentile == Fraction(3, 7)


def test_fine_group_one_below_the_minimum_falls_back_to_the_coarse_level():
    base = {**_base(107002, 7), **_base(107004, 3)}
    assignment = assign_peers("S107002_0", 107002, 5.0, base, GROUPS)
    assert assignment.level == LEVEL_COARSE
    assert assignment.group_key == 107  # sectorCode // 1000
    assert assignment.n == 10
    assert assignment.n_fine == 7
    assert assignment.n_coarse == 10
    # the coarse group holds 7 values 5..11 and 3 values 5..7: three values <= 5.0 -> ties
    assert assignment.rank == 1


def test_coarse_group_exactly_at_the_minimum_is_used():
    base = {**_base(107002, 4), **_base(107004, 4)}
    assignment = assign_peers("S107002_0", 107002, 5.0, base, GROUPS)
    assert assignment.level == LEVEL_COARSE
    assert assignment.n == 8


def test_coarse_group_one_below_the_minimum_falls_back_to_absolute():
    base = {**_base(107002, 4), **_base(107004, 3)}
    assignment = assign_peers("S107002_0", 107002, 5.0, base, GROUPS)
    assert assignment.level == LEVEL_ABSOLUTE
    assert assignment.group_key is None
    assert assignment.rank is None
    assert assignment.percentile is None
    assert assignment.n is None
    assert (assignment.n_fine, assignment.n_coarse) == (4, 7)  # the counts stay visible


def test_other_coarse_groups_do_not_count():
    base = {**_base(107002, 4), **_base(110001, 20)}
    assert assign_peers("S107002_0", 107002, 5.0, base, GROUPS).level == LEVEL_ABSOLUTE


def test_the_minimum_size_is_read_from_the_config():
    config = make_config(peer_groups={"min_size": 3})
    assert assign_peers("S107002_0", 107002, 5.0, _base(107002, 3), config.peer_groups).level == LEVEL_FINE


def test_the_coarse_divisor_is_read_from_the_config():
    config = make_config(peer_groups={"coarse_level_divisor": 100})
    base = {**_base(107002, 4), **_base(107004, 4)}
    # divisor 100: 107002 // 100 = 1070 and 107004 // 100 = 1070 -> same coarse group
    assert assign_peers("S107002_0", 107002, 5.0, base, config.peer_groups).level == LEVEL_COARSE
    base = {**_base(107002, 4), **_base(107104, 4)}
    # 107104 // 100 = 1071: a different group under this divisor
    assert assign_peers("S107002_0", 107002, 5.0, base, config.peer_groups).level == LEVEL_ABSOLUTE


def test_assignment_is_independent_of_the_base_order():
    base = {**_base(107002, 5), **_base(107004, 5)}
    reversed_base = dict(reversed(list(base.items())))
    assert assign_peers("S107002_2", 107002, 7.0, base, GROUPS) == assign_peers("S107002_2", 107002, 7.0, reversed_base, GROUPS)


def test_ticker_not_in_the_base_is_a_caller_error():
    with pytest.raises(ValueError, match="NOPE"):
        assign_peers("NOPE", 107002, 5.0, _base(107002, 8), GROUPS)


def test_value_that_differs_from_the_base_entry_is_a_caller_error():
    with pytest.raises(ValueError, match="S107002_0"):
        assign_peers("S107002_0", 107002, 99.0, _base(107002, 8), GROUPS)


def test_sector_that_differs_from_the_base_entry_is_a_caller_error():
    with pytest.raises(ValueError, match="S107002_0"):
        assign_peers("S107002_0", 110001, 5.0, _base(107002, 8), GROUPS)
