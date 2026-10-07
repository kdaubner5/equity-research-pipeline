"""The two plausibility groups of config/stage2.yaml (docs/decisions.md "Stufe 2 -
Datenplausibilitaet: Aktienzahl und Investitionen", Abschnitt 4 und 5, Test 3)."""

import pytest
import yaml

from config_layer.stage2 import DEFAULT_STAGE2_PATH, Stage2ConfigError, load_stage2_config

NEW_KEYS = [
    ("share_count_check", "suspect_ratio_above"),
    ("share_count_check", "monthly_band_low"),
    ("share_count_check", "monthly_band_high"),
    ("other_investing_check", "min_ratio_to_capex"),
]


def _real_data():
    return yaml.safe_load(DEFAULT_STAGE2_PATH.read_text(encoding="utf-8"))


def _load(tmp_path, data):
    path = tmp_path / "stage2.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_stage2_config(path)


def test_real_config_carries_the_measured_start_values():
    config = load_stage2_config()
    assert config.share_count_check.suspect_ratio_above == 6.0
    assert (config.share_count_check.monthly_band_low, config.share_count_check.monthly_band_high) == (9.0, 15.0)
    assert config.other_investing_check.min_ratio_to_capex == 2.0


@pytest.mark.parametrize("group,key", NEW_KEYS)
def test_missing_key_aborts_naming_it(tmp_path, group, key):
    data = _real_data()
    del data[group][key]
    with pytest.raises(Stage2ConfigError, match=f"{group}.{key}"):
        _load(tmp_path, data)


@pytest.mark.parametrize("group", ["share_count_check", "other_investing_check"])
def test_missing_group_aborts_naming_it(tmp_path, group):
    data = _real_data()
    del data[group]
    with pytest.raises(Stage2ConfigError, match=group):
        _load(tmp_path, data)


def test_unknown_key_in_a_new_group_aborts_naming_it(tmp_path):
    data = _real_data()
    data["share_count_check"]["lower_bound"] = 0.5
    with pytest.raises(Stage2ConfigError, match="share_count_check.lower_bound"):
        _load(tmp_path, data)


@pytest.mark.parametrize("group,key", NEW_KEYS)
@pytest.mark.parametrize("bad", [True, "6", None, [6]])
def test_non_numbers_are_rejected(tmp_path, group, key, bad):
    data = _real_data()
    data[group][key] = bad
    with pytest.raises(Stage2ConfigError, match=f"{group}.{key}"):
        _load(tmp_path, data)


@pytest.mark.parametrize("group,key,value", [
    ("share_count_check", "suspect_ratio_above", 0),
    ("share_count_check", "suspect_ratio_above", -1.0),
    ("share_count_check", "monthly_band_high", 100000),
    ("other_investing_check", "min_ratio_to_capex", 0),
    ("other_investing_check", "min_ratio_to_capex", -2.0),
])
def test_values_outside_the_plausible_range_are_rejected(tmp_path, group, key, value):
    data = _real_data()
    data[group][key] = value
    with pytest.raises(Stage2ConfigError, match=f"{group}.{key}"):
        _load(tmp_path, data)


def test_the_monthly_band_must_be_ordered(tmp_path):
    data = _real_data()
    data["share_count_check"]["monthly_band_low"] = 15.0
    data["share_count_check"]["monthly_band_high"] = 15.0
    with pytest.raises(Stage2ConfigError, match="monthly_band_low"):
        _load(tmp_path, data)


def test_the_band_must_lie_above_the_suspect_threshold(tmp_path):
    data = _real_data()
    data["share_count_check"]["suspect_ratio_above"] = 9.0  # band starts at 9.0: it would be ineffective
    with pytest.raises(Stage2ConfigError, match="monthly_band_low"):
        _load(tmp_path, data)
    data["share_count_check"]["suspect_ratio_above"] = 10.0
    with pytest.raises(Stage2ConfigError, match="monthly_band_low"):
        _load(tmp_path, data)


def test_integers_are_accepted_where_floats_are_documented(tmp_path):
    data = _real_data()
    data["share_count_check"]["suspect_ratio_above"] = 5
    data["other_investing_check"]["min_ratio_to_capex"] = 3
    config = _load(tmp_path, data)
    assert config.share_count_check.suspect_ratio_above == 5.0 and isinstance(config.share_count_check.suspect_ratio_above, float)
    assert config.other_investing_check.min_ratio_to_capex == 3.0
