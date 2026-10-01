"""config/sector_profiles.yaml loading and validation (Issue #51, docs/decisions.md
"Phase 2.0e", Entscheidung 1): same contract as thresholds.yaml - every key is
required, no code-side defaults, any problem aborts loading with a message that
names the offending key."""

import json
import re
from pathlib import Path

import pytest

from config_layer.sector_profiles import (
    DEFAULT_SECTOR_PROFILES_PATH,
    SectorProfilesConfigError,
    load_sector_profiles,
)

VALID_YAML = """
profiles:
  non_financial:
    supported: true
  banks:
    supported: false
    reason: "Banken (D2)"
sectors:
  100001: {profile: non_financial, borderline: false}
  104001: {profile: non_financial, borderline: true}
  104002: {profile: banks, borderline: false}
"""


def write(tmp_path, text):
    path = tmp_path / "sector_profiles.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_config_loads_profiles_and_sector_assignments(tmp_path):
    config = load_sector_profiles(write(tmp_path, VALID_YAML))

    assert set(config.profiles) == {"non_financial", "banks"}
    assert config.profiles["non_financial"].supported is True
    assert config.profiles["non_financial"].reason is None
    assert config.profiles["banks"].supported is False
    assert config.profiles["banks"].reason == "Banken (D2)"

    plain = config.assignment(100001)
    assert (plain.profile.name, plain.borderline) == ("non_financial", False)
    borderline = config.assignment(104001)
    assert (borderline.profile.name, borderline.borderline) == ("non_financial", True)
    excluded = config.assignment(104002)
    assert (excluded.profile.name, excluded.profile.supported, excluded.borderline) == ("banks", False, False)


def test_assignment_of_an_unknown_or_null_code_is_none_never_a_default(tmp_path):
    config = load_sector_profiles(write(tmp_path, VALID_YAML))
    assert config.assignment(999999) is None
    assert config.assignment(None) is None


def test_find_uncovered_reports_unknown_codes_sorted_and_counts_nulls(tmp_path):
    config = load_sector_profiles(write(tmp_path, VALID_YAML))
    unknown, null_count = config.find_uncovered([104002, 555, None, 100001, 111, 555, None, None])
    assert unknown == [111, 555]
    assert null_count == 3
    assert config.find_uncovered([100001, 104002]) == ([], 0)


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("", "mapping"),
        ("- a\n- b\n", "mapping"),
        ("profiles: {}\n", "sectors"),  # missing top-level key
        ("sectors: {}\n", "profiles"),
        (VALID_YAML + "extra: 1\n", "extra"),  # unrecognized top-level key
        ("profiles: {}\nsectors: {}\n", "profiles"),  # no profiles at all
        ("profiles:\n  a: {supported: true}\nsectors: {}\n", "sectors"),  # no sectors at all
    ],
)
def test_structural_problems_abort_loading(tmp_path, text, fragment):
    with pytest.raises(SectorProfilesConfigError, match=fragment):
        load_sector_profiles(write(tmp_path, text))


def test_missing_file_and_invalid_yaml_abort_loading(tmp_path):
    with pytest.raises(SectorProfilesConfigError, match="could not read"):
        load_sector_profiles(tmp_path / "nope.yaml")
    with pytest.raises(SectorProfilesConfigError, match="not valid YAML"):
        load_sector_profiles(write(tmp_path, "profiles: [unclosed\n"))


@pytest.mark.parametrize(
    "profile_yaml,fragment",
    [
        ("  a: {}\n", "a.supported"),  # supported is required
        ("  a: {supported: yes_please}\n", "a.supported"),  # not a bool
        ("  a: {supported: 1}\n", "a.supported"),  # an int is not a bool
        ("  a: {supported: false}\n", "a.reason"),  # unsupported needs a reason
        ("  a: {supported: false, reason: ''}\n", "a.reason"),  # ... a non-empty one
        ("  a: {supported: false, reason: 5}\n", "a.reason"),
        ("  a: {supported: true, reason: 'why'}\n", "a.reason"),  # supported must not carry one
        ("  a: {supported: true, colour: red}\n", "colour"),  # unrecognized key
        ("  a: true\n", "a"),  # profile is not a mapping
    ],
)
def test_profile_validation_names_the_offending_key(tmp_path, profile_yaml, fragment):
    text = "profiles:\n" + profile_yaml + "sectors:\n  1: {profile: a, borderline: false}\n"
    with pytest.raises(SectorProfilesConfigError, match=fragment):
        load_sector_profiles(write(tmp_path, text))


@pytest.mark.parametrize(
    "sector_yaml,fragment",
    [
        ("  100001: {borderline: false}\n", "100001.profile"),
        ("  100001: {profile: non_financial}\n", "100001.borderline"),  # borderline is required too
        ("  100001: {profile: ghost, borderline: false}\n", "ghost"),  # unknown profile
        ("  100001: {profile: non_financial, borderline: maybe}\n", "100001.borderline"),
        ("  100001: {profile: non_financial, borderline: false, x: 1}\n", "x"),  # unrecognized key
        ("  100001: non_financial\n", "100001"),  # not a mapping
        ("  abc: {profile: non_financial, borderline: false}\n", "abc"),  # code is not an integer
        ("  true: {profile: non_financial, borderline: false}\n", "True"),  # bool is not a code
        ("  1.5: {profile: non_financial, borderline: false}\n", "1.5"),
        ("  104002: {profile: banks, borderline: true}\n", "104002.borderline"),  # borderline only on supported
    ],
)
def test_sector_validation_names_the_offending_key(tmp_path, sector_yaml, fragment):
    head = (
        "profiles:\n  non_financial: {supported: true}\n  banks: {supported: false, reason: r}\nsectors:\n"
    )
    with pytest.raises(SectorProfilesConfigError, match=fragment):
        load_sector_profiles(write(tmp_path, head + sector_yaml))


def test_a_duplicate_sector_code_is_rejected_not_silently_overwritten(tmp_path):
    text = VALID_YAML + "  104002: {profile: non_financial, borderline: false}\n"
    with pytest.raises(SectorProfilesConfigError, match="duplicate.*104002"):
        load_sector_profiles(write(tmp_path, text))


def test_a_duplicate_profile_name_is_rejected(tmp_path):
    text = VALID_YAML.replace(
        "  banks:\n    supported: false\n",
        "  non_financial:\n    supported: true\n  banks:\n    supported: false\n",
    )
    with pytest.raises(SectorProfilesConfigError, match="duplicate.*non_financial"):
        load_sector_profiles(write(tmp_path, text))


# --- the real config/sector_profiles.yaml ---------------------------------------------

EXCLUDED = {
    104002: "banks",
    104004: "insurance",
    104005: "insurance",
    104006: "insurance",
    104013: "insurance",
    109001: "reits",
    106002: "biotechnology",
}
BORDERLINE = {104001, 104003, 104007, 109002}
GENERAL_BULK = Path(__file__).parent.parent / "data" / "cache" / "universe" / "2026-09-24" / "general_bulk.json"


def test_real_config_loads_and_assigns_the_excluded_groups_with_a_reason():
    config = load_sector_profiles(DEFAULT_SECTOR_PROFILES_PATH)
    for code, profile_name in EXCLUDED.items():
        assignment = config.assignment(code)
        assert assignment.profile.name == profile_name, code
        assert assignment.profile.supported is False
        assert assignment.profile.reason  # every exclusion carries its reason
        assert assignment.borderline is False
    assert config.profiles["non_financial"].supported is True


def test_real_config_marks_exactly_the_borderline_codes_and_keeps_them_in_the_standard_profile():
    config = load_sector_profiles(DEFAULT_SECTOR_PROFILES_PATH)
    borderline = {code for code, a in config.sectors.items() if a.borderline}
    assert borderline == BORDERLINE
    for code in BORDERLINE:
        assert config.assignment(code).profile.name == "non_financial"
    # every other code is either excluded or plain standard
    for code, assignment in config.sectors.items():
        if code not in EXCLUDED and code not in BORDERLINE:
            assert assignment.profile.name == "non_financial", code


@pytest.mark.skipif(not GENERAL_BULK.exists(), reason="data/cache/universe is gitignored and absent in this checkout")
def test_real_config_lists_every_sector_code_of_the_universe_bulk_file():
    with open(GENERAL_BULK, encoding="utf-8") as f:
        response = json.load(f)["response"]
    index = response["columns"].index("sectorCode")
    codes = {row[index] for row in response["data"] if row[index] is not None}
    config = load_sector_profiles(DEFAULT_SECTOR_PROFILES_PATH)
    assert set(config.sectors) == codes


# --- hardening from the /code-review of PR #53 ----------------------------------------


@pytest.mark.parametrize("profile_value", ["[banks]", "5", "null", "{a: b}", "true"])
def test_a_sector_profile_that_is_not_a_string_is_a_config_error_naming_the_key_not_a_type_error(tmp_path, profile_value):
    text = (
        "profiles:\n  non_financial: {supported: true}\n  banks: {supported: false, reason: r}\nsectors:\n"
        f"  104002: {{profile: {profile_value}, borderline: false}}\n"
    )
    with pytest.raises(SectorProfilesConfigError, match=r"sectors\.104002\.profile"):
        load_sector_profiles(write(tmp_path, text))


@pytest.mark.parametrize("second_spelling", ["104_002", "0x19642", "+104002"])
def test_the_same_sector_code_in_another_spelling_is_a_duplicate(tmp_path, second_spelling):
    # YAML resolves all of these to the integer 104002; the dict would silently keep only the last
    text = VALID_YAML + f"  {second_spelling}: {{profile: non_financial, borderline: false}}\n"
    with pytest.raises(SectorProfilesConfigError) as excinfo:
        load_sector_profiles(write(tmp_path, text))
    message = str(excinfo.value)
    assert "duplicate" in message and "104002" in message
    match = re.search(r"lines (\d+) and (\d+)", message)  # the message names both lines
    assert match and int(match.group(1)) != int(match.group(2))
    assert second_spelling in message  # ... and the second spelling, which differs from the first
