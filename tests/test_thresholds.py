import hashlib

import pytest

from config_layer.thresholds import (
    DEFAULT_THRESHOLDS_PATH,
    ThresholdsConfigError,
    compute_config_hash,
    load_thresholds,
)

VALID_YAML = """\
roic:
  nopat_tax_rate: 0.25
  ic_asset_floor_ratio: 0.10
  implausible_above: 0.60
  clean_pass_above: 0.12
  fail_below: 0.10
net_debt_ebitda:
  clean_pass_below: 2.3
  fail_at_or_above: 2.5
cash_conversion:
  clean_pass_at_or_above: 1.0
  fail_below: 0.8
net_income_margin:
  fail_below: 0.02
data_quality_reconciliation:
  debt_tolerance_ratio: 0.05
  debt_tolerance_assets_ratio: 0.001
"""


def _write(tmp_path, text, name="thresholds.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_file_loads_correctly(tmp_path):
    thresholds = load_thresholds(_write(tmp_path, VALID_YAML))
    assert thresholds.roic.clean_pass_above == 0.12
    assert thresholds.roic.fail_below == 0.10
    assert thresholds.roic.nopat_tax_rate == 0.25
    assert thresholds.roic.ic_asset_floor_ratio == 0.10
    assert thresholds.roic.implausible_above == 0.60
    assert thresholds.net_debt_ebitda.clean_pass_below == 2.3
    assert thresholds.net_debt_ebitda.fail_at_or_above == 2.5
    assert thresholds.cash_conversion.clean_pass_at_or_above == 1.0
    assert thresholds.cash_conversion.fail_below == 0.8
    assert thresholds.net_income_margin.fail_below == 0.02
    assert thresholds.data_quality_reconciliation.debt_tolerance_ratio == 0.05
    assert thresholds.data_quality_reconciliation.debt_tolerance_assets_ratio == 0.001


def test_the_committed_thresholds_yaml_is_itself_valid():
    # Regression guard: config/thresholds.yaml (loaded via conftest's
    # DEFAULT_THRESHOLDS at import time already) must load without error
    # through the default path too.
    assert load_thresholds(DEFAULT_THRESHOLDS_PATH) is not None


@pytest.mark.parametrize(
    "missing_line,dotted_key",
    [
        ("  nopat_tax_rate: 0.25\n", "roic.nopat_tax_rate"),
        ("  ic_asset_floor_ratio: 0.10\n", "roic.ic_asset_floor_ratio"),
        ("  implausible_above: 0.60\n", "roic.implausible_above"),
        ("  clean_pass_above: 0.12\n", "roic.clean_pass_above"),
        ("  fail_below: 0.10\n", "roic.fail_below"),
        ("  clean_pass_below: 2.3\n", "net_debt_ebitda.clean_pass_below"),
        ("  fail_at_or_above: 2.5\n", "net_debt_ebitda.fail_at_or_above"),
        ("  clean_pass_at_or_above: 1.0\n", "cash_conversion.clean_pass_at_or_above"),
        ("  debt_tolerance_ratio: 0.05\n", "data_quality_reconciliation.debt_tolerance_ratio"),
    ],
)
def test_missing_key_aborts_naming_the_dotted_key(tmp_path, missing_line, dotted_key):
    text = VALID_YAML.replace(missing_line, "")
    with pytest.raises(ThresholdsConfigError, match=dotted_key.replace(".", r"\.")):
        load_thresholds(_write(tmp_path, text))


def test_missing_entire_group_aborts_naming_it(tmp_path):
    text = VALID_YAML.replace(
        "net_income_margin:\n  fail_below: 0.02\n", ""
    )
    with pytest.raises(ThresholdsConfigError, match="net_income_margin"):
        load_thresholds(_write(tmp_path, text))


def test_unknown_top_level_key_aborts_naming_it(tmp_path):
    text = VALID_YAML + "trend_guard:\n  min_growth: 0.0\n"
    with pytest.raises(ThresholdsConfigError, match="trend_guard"):
        load_thresholds(_write(tmp_path, text))


def test_unknown_key_within_a_known_group_aborts_naming_it(tmp_path):
    text = VALID_YAML.replace(
        "  fail_below: 0.10\nnet_debt_ebitda:",
        "  fail_below: 0.10\n  extra_key: 1.0\nnet_debt_ebitda:",
    )
    with pytest.raises(ThresholdsConfigError, match="roic.extra_key"):
        load_thresholds(_write(tmp_path, text))


@pytest.mark.parametrize(
    "bad_value",
    ["yes", "true", "\"0.12\"", "[0.12]", "null"],
)
def test_non_numeric_value_aborts_naming_the_key(tmp_path, bad_value):
    text = VALID_YAML.replace("  clean_pass_above: 0.12\n", f"  clean_pass_above: {bad_value}\n")
    with pytest.raises(ThresholdsConfigError, match=r"roic\.clean_pass_above"):
        load_thresholds(_write(tmp_path, text))


@pytest.mark.parametrize("bad_value", [-0.5, 15.0])
def test_out_of_range_value_aborts_naming_the_key(tmp_path, bad_value):
    text = VALID_YAML.replace("  clean_pass_above: 0.12\n", f"  clean_pass_above: {bad_value}\n")
    with pytest.raises(ThresholdsConfigError, match=r"roic\.clean_pass_above"):
        load_thresholds(_write(tmp_path, text))


def test_cash_conversion_thresholds_above_1_0_are_valid_not_out_of_range(tmp_path):
    # Regression guard: CFO/Net-Income can legitimately exceed 1.0
    # (masterplan.md §4 even names 2.0 as a ranking cap) - the range check
    # must not reject a value like 1.5 as if it were a data-entry slip.
    text = VALID_YAML.replace(
        "  clean_pass_at_or_above: 1.0\n  fail_below: 0.8\n",
        "  clean_pass_at_or_above: 1.5\n  fail_below: 1.2\n",
    )
    thresholds = load_thresholds(_write(tmp_path, text))
    assert thresholds.cash_conversion.clean_pass_at_or_above == 1.5
    assert thresholds.cash_conversion.fail_below == 1.2


def test_roic_grey_zone_ordering_violation_aborts_naming_both_keys(tmp_path):
    text = VALID_YAML.replace("  clean_pass_above: 0.12\n", "  clean_pass_above: 0.09\n")
    with pytest.raises(ThresholdsConfigError, match=r"roic\.clean_pass_above.*roic\.fail_below"):
        load_thresholds(_write(tmp_path, text))


def test_net_debt_ebitda_grey_zone_ordering_violation_aborts_naming_both_keys(tmp_path):
    text = VALID_YAML.replace("  clean_pass_below: 2.3\n", "  clean_pass_below: 2.6\n")
    with pytest.raises(
        ThresholdsConfigError, match=r"net_debt_ebitda\.clean_pass_below.*net_debt_ebitda\.fail_at_or_above"
    ):
        load_thresholds(_write(tmp_path, text))


def test_cash_conversion_grey_zone_ordering_violation_aborts_naming_both_keys(tmp_path):
    text = VALID_YAML.replace("  clean_pass_at_or_above: 1.0\n", "  clean_pass_at_or_above: 0.7\n")
    with pytest.raises(
        ThresholdsConfigError,
        match=r"cash_conversion\.clean_pass_at_or_above.*cash_conversion\.fail_below",
    ):
        load_thresholds(_write(tmp_path, text))


def test_missing_file_raises(tmp_path):
    with pytest.raises(ThresholdsConfigError):
        load_thresholds(tmp_path / "does_not_exist.yaml")


def test_hash_computation_matches_independent_sha256(tmp_path):
    path = _write(tmp_path, VALID_YAML)
    expected = hashlib.sha256(VALID_YAML.encode("utf-8")).hexdigest()
    digest = compute_config_hash(path)
    assert digest == expected
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_is_stable_for_the_same_file(tmp_path):
    path = _write(tmp_path, VALID_YAML)
    assert compute_config_hash(path) == compute_config_hash(path)


def test_hash_changes_for_a_different_file(tmp_path):
    path_a = _write(tmp_path, VALID_YAML, "a.yaml")
    path_b = _write(tmp_path, VALID_YAML.replace("0.12", "0.13"), "b.yaml")
    assert compute_config_hash(path_a) != compute_config_hash(path_b)


def test_hash_is_identical_for_lf_and_crlf_versions_of_the_same_content(tmp_path):
    lf_path = tmp_path / "lf.yaml"
    crlf_path = tmp_path / "crlf.yaml"
    lf_path.write_bytes(VALID_YAML.encode("utf-8"))
    crlf_path.write_bytes(VALID_YAML.replace("\n", "\r\n").encode("utf-8"))
    assert compute_config_hash(lf_path) == compute_config_hash(crlf_path)
