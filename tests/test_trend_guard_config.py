"""config/trend_guard.yaml loading and validation (docs/decisions.md "Trend-Guard", Abschnitte 4 und 8, Test 3)."""

import copy

import pytest
import yaml

from config_layer.trend_guard import (
    DEFAULT_TREND_GUARD_PATH,
    TrendGuardConfigError,
    compute_trend_guard_config_hash,
    load_trend_guard_config,
)

LEAF_KEYS = [
    "years",
    "min_years",
    "revenue_ratio_flag_below",
    "ebitda_margin_ratio_flag_below",
    "ebitda_margin_ratio_flag_above",
]


def _real_data():
    return yaml.safe_load(DEFAULT_TREND_GUARD_PATH.read_text(encoding="utf-8"))


def _load(tmp_path, data):
    path = tmp_path / "trend_guard.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_trend_guard_config(path)


def _with(**changes):
    data = copy.deepcopy(_real_data())
    data["trend_guard"].update(changes)
    return data


def test_the_real_file_loads_with_the_decided_start_values():
    config = load_trend_guard_config()
    assert config.years == 3  # Q3: 3 Jahre
    assert config.min_years == 3
    assert config.revenue_ratio_flag_below == 0.90
    assert config.ebitda_margin_ratio_flag_below == 0.90
    assert config.ebitda_margin_ratio_flag_above == 1.25  # T4, Q4


def test_the_real_file_has_exactly_the_documented_keys():
    assert sorted(_real_data()) == ["trend_guard"]
    assert sorted(_real_data()["trend_guard"]) == sorted(LEAF_KEYS)


@pytest.mark.parametrize("key", LEAF_KEYS)
def test_a_missing_leaf_key_aborts_naming_it(tmp_path, key):
    data = _real_data()
    del data["trend_guard"][key]
    with pytest.raises(TrendGuardConfigError, match=f"trend_guard.{key}"):
        _load(tmp_path, data)


def test_an_unknown_leaf_key_aborts_naming_it(tmp_path):
    with pytest.raises(TrendGuardConfigError, match="trend_guard.extra"):
        _load(tmp_path, _with(extra=1))


def test_an_unknown_top_level_key_aborts(tmp_path):
    data = _real_data()
    data["other"] = {}
    with pytest.raises(TrendGuardConfigError, match="other"):
        _load(tmp_path, data)


def test_a_missing_group_aborts(tmp_path):
    with pytest.raises(TrendGuardConfigError, match="trend_guard"):
        _load(tmp_path, {})


def test_the_group_must_be_a_mapping(tmp_path):
    with pytest.raises(TrendGuardConfigError, match="mapping"):
        _load(tmp_path, {"trend_guard": [1, 2]})


@pytest.mark.parametrize("key", LEAF_KEYS)
@pytest.mark.parametrize("bad", ["0.9", True, None, [1]])
def test_a_non_number_aborts_naming_the_key(tmp_path, key, bad):
    with pytest.raises(TrendGuardConfigError, match=f"trend_guard.{key}"):
        _load(tmp_path, _with(**{key: bad}))


@pytest.mark.parametrize("key", ["years", "min_years"])
def test_a_float_where_an_integer_is_required_aborts(tmp_path, key):
    with pytest.raises(TrendGuardConfigError, match=f"trend_guard.{key}.*integer"):
        _load(tmp_path, _with(**{key: 3.0}))


@pytest.mark.parametrize("years", [2, 6, 0, -1])
def test_years_must_lie_between_3_and_5(tmp_path, years):
    with pytest.raises(TrendGuardConfigError, match="trend_guard.years"):
        _load(tmp_path, _with(years=years))


@pytest.mark.parametrize("years", [3, 4, 5])
def test_years_3_to_5_are_accepted(tmp_path, years):
    assert _load(tmp_path, _with(years=years, min_years=3)).years == years


def test_min_years_may_not_exceed_years(tmp_path):
    with pytest.raises(TrendGuardConfigError, match="min_years"):
        _load(tmp_path, _with(years=3, min_years=4))


def test_min_years_equal_to_years_is_accepted(tmp_path):
    assert _load(tmp_path, _with(years=4, min_years=4)).min_years == 4


def test_min_years_below_2_aborts(tmp_path):
    with pytest.raises(TrendGuardConfigError, match="trend_guard.min_years"):
        _load(tmp_path, _with(min_years=1))


@pytest.mark.parametrize("key", ["revenue_ratio_flag_below", "ebitda_margin_ratio_flag_below"])
@pytest.mark.parametrize("value", [0, 0.0, -0.5, 1.0, 1.5])
def test_the_flag_below_thresholds_must_lie_strictly_between_0_and_1(tmp_path, key, value):
    with pytest.raises(TrendGuardConfigError, match=f"trend_guard.{key}"):
        _load(tmp_path, _with(**{key: value}))


@pytest.mark.parametrize("value", [1.0, 0.9, 0, -2.0])
def test_the_flag_above_threshold_must_exceed_1(tmp_path, value):
    with pytest.raises(TrendGuardConfigError, match="trend_guard.ebitda_margin_ratio_flag_above"):
        _load(tmp_path, _with(ebitda_margin_ratio_flag_above=value))


def test_the_flag_above_threshold_has_a_plausibility_ceiling(tmp_path):
    with pytest.raises(TrendGuardConfigError, match="trend_guard.ebitda_margin_ratio_flag_above"):
        _load(tmp_path, _with(ebitda_margin_ratio_flag_above=125.0))  # 1.25 typed as 125


def test_a_duplicate_key_aborts(tmp_path):
    path = tmp_path / "trend_guard.yaml"
    path.write_text("trend_guard:\n  years: 3\n  years: 4\n", encoding="utf-8")
    with pytest.raises(TrendGuardConfigError, match="years"):
        load_trend_guard_config(path)


def test_invalid_yaml_and_a_missing_file_abort(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("trend_guard: [unclosed", encoding="utf-8")
    with pytest.raises(TrendGuardConfigError):
        load_trend_guard_config(bad)
    with pytest.raises(TrendGuardConfigError, match="could not read"):
        load_trend_guard_config(tmp_path / "missing.yaml")


def test_the_hash_ignores_line_endings_and_changes_with_content(tmp_path):
    text = DEFAULT_TREND_GUARD_PATH.read_text(encoding="utf-8")
    unix, windows, changed = tmp_path / "u.yaml", tmp_path / "w.yaml", tmp_path / "c.yaml"
    unix.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
    windows.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
    changed.write_bytes(text.replace("1.25", "1.30").encode("utf-8"))
    assert compute_trend_guard_config_hash(unix) == compute_trend_guard_config_hash(windows)
    assert compute_trend_guard_config_hash(unix) != compute_trend_guard_config_hash(changed)
