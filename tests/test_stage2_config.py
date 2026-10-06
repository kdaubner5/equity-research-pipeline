"""config/stage2.yaml loading and validation (docs/decisions.md "Stufe 2: Peer-Gruppen
und relative Bewertung", Abschnitte 5 und 6, Test 4)."""

import copy
from fractions import Fraction

import pytest
import yaml

from config_layer.sector_profiles import load_sector_profiles
from config_layer.stage2 import DEFAULT_STAGE2_PATH, Stage2ConfigError, compute_stage2_config_hash, load_stage2_config

LEAF_KEYS = [
    ("peer_groups", "min_size"),
    ("peer_groups", "coarse_level_divisor"),
    ("ev_ebitda", "implausible_above"),
    ("ev_ebitda", "peer_terciles"),
    ("ev_ebitda", "grey_zone_upper_percentile"),
    ("ev_ebitda", "absolute_clean_pass_below"),
    ("ev_ebitda", "absolute_fail_at_or_above"),
    ("ev_ebitda", "normalized_ebitda_years"),
    ("fcf_yield", "years"),
    ("fcf_yield", "clean_pass_above"),
    ("fcf_yield", "fail_below"),
    ("fcf_yield", "implausible_above"),
    ("fundamentals", "max_fiscal_year_lag"),
]


def _real_data():
    return yaml.safe_load(DEFAULT_STAGE2_PATH.read_text(encoding="utf-8"))


def _load(tmp_path, data):
    path = tmp_path / "stage2.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_stage2_config(path)


def test_real_config_loads_with_the_documented_start_values():
    config = load_stage2_config()
    assert config.peer_groups.min_size == 8  # Masterplan: n >= 8
    assert config.peer_groups.coarse_level_divisor == 1000
    assert config.ev_ebitda.peer_terciles == 3  # "guenstigstes Drittel"
    assert config.fcf_yield.clean_pass_above == 0.06  # Masterplan: FCF-Yield > 6 %
    assert config.cyclical_sectors.sector_codes == (107002, 107004, 110001, 110002, 110005, 110006)  # Issue #52


def test_real_config_has_the_values_in_the_documented_types():
    config = load_stage2_config()
    assert isinstance(config.peer_groups.min_size, int)
    assert isinstance(config.ev_ebitda.peer_terciles, int)
    assert isinstance(config.fcf_yield.years, int)
    assert isinstance(config.ev_ebitda.implausible_above, float)
    assert 1 / config.ev_ebitda.peer_terciles < config.ev_ebitda.grey_zone_upper_percentile <= 1


def test_every_cyclical_sector_code_is_listed_in_a_supported_profile_of_sector_profiles_yaml():
    """Cross-check: a typo in a cyclical code would otherwise flag nothing, silently."""
    profiles = load_sector_profiles()
    for code in load_stage2_config().cyclical_sectors.sector_codes:
        assignment = profiles.assignment(code)
        assert assignment is not None, f"cyclical sector code {code} is not listed in config/sector_profiles.yaml"
        assert assignment.profile.supported, f"cyclical sector code {code} belongs to an unsupported profile"


@pytest.mark.parametrize("group,key", LEAF_KEYS)
def test_missing_leaf_key_aborts_naming_it(tmp_path, group, key):
    data = _real_data()
    del data[group][key]
    with pytest.raises(Stage2ConfigError, match=f"{group}.{key}"):
        _load(tmp_path, data)


@pytest.mark.parametrize("group", ["peer_groups", "ev_ebitda", "fcf_yield", "fundamentals", "cyclical_sectors"])
def test_missing_group_aborts_naming_it(tmp_path, group):
    data = _real_data()
    del data[group]
    with pytest.raises(Stage2ConfigError, match=group):
        _load(tmp_path, data)


def test_missing_cyclical_codes_key_aborts(tmp_path):
    data = _real_data()
    data["cyclical_sectors"] = {}
    with pytest.raises(Stage2ConfigError, match="cyclical_sectors.sector_codes"):
        _load(tmp_path, data)


def test_unknown_top_level_key_aborts_naming_it(tmp_path):
    data = _real_data()
    data["trend_guard"] = {"x": 1}
    with pytest.raises(Stage2ConfigError, match="trend_guard"):
        _load(tmp_path, data)


def test_unknown_leaf_key_aborts_naming_it(tmp_path):
    data = _real_data()
    data["fcf_yield"]["min_yield"] = 0.01
    with pytest.raises(Stage2ConfigError, match="fcf_yield.min_yield"):
        _load(tmp_path, data)


def test_duplicate_key_is_rejected(tmp_path):
    path = tmp_path / "stage2.yaml"
    text = DEFAULT_STAGE2_PATH.read_text(encoding="utf-8")
    path.write_text(text + "\nfcf_yield:\n  years: 1\n", encoding="utf-8")
    with pytest.raises(Stage2ConfigError, match="duplicate"):
        load_stage2_config(path)


def test_missing_file_aborts(tmp_path):
    with pytest.raises(Stage2ConfigError, match="could not read"):
        load_stage2_config(tmp_path / "nope.yaml")


def test_invalid_yaml_aborts(tmp_path):
    path = tmp_path / "stage2.yaml"
    path.write_text("peer_groups: [unclosed", encoding="utf-8")
    with pytest.raises(Stage2ConfigError, match="not valid YAML"):
        load_stage2_config(path)


@pytest.mark.parametrize("bad", [True, "8", None, [8]])
def test_non_number_is_rejected(tmp_path, bad):
    data = _real_data()
    data["peer_groups"]["min_size"] = bad
    with pytest.raises(Stage2ConfigError, match="peer_groups.min_size"):
        _load(tmp_path, data)


@pytest.mark.parametrize(
    "group,key",
    [
        ("peer_groups", "min_size"),
        ("peer_groups", "coarse_level_divisor"),
        ("ev_ebitda", "peer_terciles"),
        ("ev_ebitda", "normalized_ebitda_years"),
        ("fcf_yield", "years"),
        ("fundamentals", "max_fiscal_year_lag"),
    ],
)
def test_fractional_value_for_an_integer_key_is_rejected(tmp_path, group, key):
    data = _real_data()
    data[group][key] = 3.5
    with pytest.raises(Stage2ConfigError, match=f"{group}.{key}.*integer"):
        _load(tmp_path, data)


@pytest.mark.parametrize(
    "group,key,value",
    [
        ("peer_groups", "min_size", 1),  # a percentile needs n >= 2
        ("peer_groups", "coarse_level_divisor", 0),
        ("ev_ebitda", "peer_terciles", 1),
        ("ev_ebitda", "implausible_above", 0),
        ("ev_ebitda", "normalized_ebitda_years", 4),  # the mapper delivers at most three years
        ("ev_ebitda", "normalized_ebitda_years", 0),
        ("fcf_yield", "years", 4),
        ("fcf_yield", "years", 0),
        ("fcf_yield", "clean_pass_above", 1.5),  # "6" instead of "0.06"
        ("fcf_yield", "implausible_above", 0),
        ("fundamentals", "max_fiscal_year_lag", -1),
        ("ev_ebitda", "grey_zone_upper_percentile", 1.2),
    ],
)
def test_value_outside_its_range_is_rejected(tmp_path, group, key, value):
    data = _real_data()
    data[group][key] = value
    with pytest.raises(Stage2ConfigError, match=f"{group}.{key}"):
        _load(tmp_path, data)


def test_grey_zone_must_lie_above_the_tercile_boundary(tmp_path):
    data = _real_data()
    data["ev_ebitda"]["peer_terciles"] = 3
    data["ev_ebitda"]["grey_zone_upper_percentile"] = 1 / 3
    with pytest.raises(Stage2ConfigError, match="grey_zone_upper_percentile"):
        _load(tmp_path, data)


def test_absolute_ev_ebitda_thresholds_must_be_ordered(tmp_path):
    data = _real_data()
    data["ev_ebitda"]["absolute_clean_pass_below"] = 10.0
    data["ev_ebitda"]["absolute_fail_at_or_above"] = 10.0
    with pytest.raises(Stage2ConfigError, match="absolute_clean_pass_below"):
        _load(tmp_path, data)


def test_fcf_yield_grey_zone_must_lie_below_the_clean_threshold(tmp_path):
    data = _real_data()
    data["fcf_yield"]["clean_pass_above"] = 0.05
    data["fcf_yield"]["fail_below"] = 0.05
    with pytest.raises(Stage2ConfigError, match="clean_pass_above"):
        _load(tmp_path, data)


def test_fcf_yield_implausible_cap_must_exceed_the_clean_threshold(tmp_path):
    data = _real_data()
    data["fcf_yield"]["implausible_above"] = 0.06
    with pytest.raises(Stage2ConfigError, match="implausible_above"):
        _load(tmp_path, data)


def test_ev_ebitda_implausible_cap_must_exceed_the_absolute_fail_threshold(tmp_path):
    data = _real_data()
    data["ev_ebitda"]["implausible_above"] = 9.0
    with pytest.raises(Stage2ConfigError, match="implausible_above"):
        _load(tmp_path, data)


def test_duplicate_cyclical_codes_are_rejected_by_parsed_value(tmp_path):
    path = tmp_path / "stage2.yaml"
    text = DEFAULT_STAGE2_PATH.read_text(encoding="utf-8")
    assert "sector_codes: [" in text
    start = text.index("sector_codes: [")
    path.write_text(text[:start] + "sector_codes: [107002, 107_002]\n", encoding="utf-8")
    with pytest.raises(Stage2ConfigError, match="107002"):
        load_stage2_config(path)


@pytest.mark.parametrize("bad", [[107002.5], ["107002"], [True], "107002", [[107002]]])
def test_cyclical_codes_must_be_a_list_of_integers(tmp_path, bad):
    data = copy.deepcopy(_real_data())
    data["cyclical_sectors"]["sector_codes"] = bad
    with pytest.raises(Stage2ConfigError, match="cyclical_sectors.sector_codes"):
        _load(tmp_path, data)


def test_empty_cyclical_list_is_allowed_but_explicit(tmp_path):
    data = _real_data()
    data["cyclical_sectors"]["sector_codes"] = []
    assert _load(tmp_path, data).cyclical_sectors.sector_codes == ()


def test_grey_zone_percentile_is_kept_exact_as_a_decimal_fraction(tmp_path):
    data = _real_data()
    data["ev_ebitda"]["grey_zone_upper_percentile"] = 0.4
    config = _load(tmp_path, data)
    assert Fraction(str(config.ev_ebitda.grey_zone_upper_percentile)) == Fraction(2, 5)


def test_config_hash_normalizes_line_endings(tmp_path):
    lf = tmp_path / "lf.yaml"
    crlf = tmp_path / "crlf.yaml"
    lf.write_bytes(b"a: 1\nb: 2\n")
    crlf.write_bytes(b"a: 1\r\nb: 2\r\n")
    assert compute_stage2_config_hash(lf) == compute_stage2_config_hash(crlf)
    assert len(compute_stage2_config_hash(lf)) == 64
