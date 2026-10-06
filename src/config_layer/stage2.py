"""Stage-2 configuration loading/validation, per docs/decisions.md ("Stufe 2: Peer-Gruppen
und relative Bewertung", Abschnitt 5).

Every parameter of the Stage-2 valuation lives in config/stage2.yaml, never as a Python
constant with a code-side default - a missing, unrecognized or duplicate key aborts
loading with the exact dotted key name (CLAUDE.md: "keine Abkürzungen bei
Datenintegrität"). The file is separate from config/thresholds.yaml on purpose: Stage 1
and its config_hash stay untouched; Stage 2 records its own hash
(compute_stage2_config_hash).
"""

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Tuple

import yaml

from config_layer.thresholds import ThresholdsConfigError, _parse_yaml, compute_config_hash

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_STAGE2_PATH = _REPO_ROOT / "config" / "stage2.yaml"

# SimFin's compact statements deliver at most MAX_YEARS (3) fiscal years per company
# (data_layer.simfin_client); a longer averaging window could never be filled.
_MAX_AVERAGING_YEARS = 3


class Stage2ConfigError(Exception):
    """Raised when config/stage2.yaml is missing, malformed, or fails validation.
    Every message names the offending dotted key - no default is ever substituted."""


@dataclass(frozen=True)
class PeerGroupsConfig:
    min_size: int
    coarse_level_divisor: int


@dataclass(frozen=True)
class EvEbitdaConfig:
    implausible_above: float
    peer_terciles: int
    grey_zone_upper_percentile: float
    absolute_clean_pass_below: float
    absolute_fail_at_or_above: float
    normalized_ebitda_years: int

    def __post_init__(self):
        # Exact fractions: 0.40 must mean exactly 2/5, and "1/3" exactly one third.
        if not Fraction(1, self.peer_terciles) < Fraction(str(self.grey_zone_upper_percentile)) <= 1:
            raise Stage2ConfigError(
                f"ev_ebitda.grey_zone_upper_percentile ({self.grey_zone_upper_percentile}) must be greater than "
                f"1/ev_ebitda.peer_terciles (1/{self.peer_terciles}) and at most 1"
            )
        if not self.absolute_clean_pass_below < self.absolute_fail_at_or_above:
            raise Stage2ConfigError(
                f"ev_ebitda.absolute_clean_pass_below ({self.absolute_clean_pass_below}) must be less than "
                f"ev_ebitda.absolute_fail_at_or_above ({self.absolute_fail_at_or_above})"
            )
        # A cap at or below the failing threshold would turn every failing multiple into "invalid".
        if not self.implausible_above > self.absolute_fail_at_or_above:
            raise Stage2ConfigError(
                f"ev_ebitda.implausible_above ({self.implausible_above}) must be greater than "
                f"ev_ebitda.absolute_fail_at_or_above ({self.absolute_fail_at_or_above})"
            )


@dataclass(frozen=True)
class FcfYieldConfig:
    years: int
    clean_pass_above: float
    fail_below: float
    implausible_above: float

    def __post_init__(self):
        if not self.clean_pass_above > self.fail_below:
            raise Stage2ConfigError(
                f"fcf_yield.clean_pass_above ({self.clean_pass_above}) must be greater than "
                f"fcf_yield.fail_below ({self.fail_below})"
            )
        if not self.implausible_above > self.clean_pass_above:
            raise Stage2ConfigError(
                f"fcf_yield.implausible_above ({self.implausible_above}) must be greater than "
                f"fcf_yield.clean_pass_above ({self.clean_pass_above})"
            )


@dataclass(frozen=True)
class FundamentalsConfig:
    max_fiscal_year_lag: int


@dataclass(frozen=True)
class CyclicalSectorsConfig:
    sector_codes: Tuple[int, ...]


@dataclass(frozen=True)
class Stage2Config:
    peer_groups: PeerGroupsConfig
    ev_ebitda: EvEbitdaConfig
    fcf_yield: FcfYieldConfig
    fundamentals: FundamentalsConfig
    cyclical_sectors: CyclicalSectorsConfig


# (group, key) -> (min, max), inclusive. Coarse plausibility guardrails against a data-entry
# slip (e.g. "6" instead of "0.06"), not a second copy of the business logic.
_SCHEMA = {
    "peer_groups": {
        "min_size": (2, 10_000),  # a percentile needs n >= 2
        "coarse_level_divisor": (1, 1_000_000),
    },
    "ev_ebitda": {
        "implausible_above": (1.0, 10_000.0),
        "peer_terciles": (2, 100),
        "grey_zone_upper_percentile": (0.0, 1.0),
        "absolute_clean_pass_below": (0.0, 1_000.0),
        "absolute_fail_at_or_above": (0.0, 1_000.0),
        "normalized_ebitda_years": (1, _MAX_AVERAGING_YEARS),
    },
    "fcf_yield": {
        "years": (1, _MAX_AVERAGING_YEARS),
        "clean_pass_above": (0.0, 1.0),
        "fail_below": (-1.0, 1.0),
        "implausible_above": (0.0, 100.0),
    },
    "fundamentals": {
        "max_fiscal_year_lag": (0, 10),
    },
}

_INTEGER_KEYS = {
    ("peer_groups", "min_size"),
    ("peer_groups", "coarse_level_divisor"),
    ("ev_ebitda", "peer_terciles"),
    ("ev_ebitda", "normalized_ebitda_years"),
    ("fcf_yield", "years"),
    ("fundamentals", "max_fiscal_year_lag"),
}

_GROUP_DATACLASSES = {
    "peer_groups": PeerGroupsConfig,
    "ev_ebitda": EvEbitdaConfig,
    "fcf_yield": FcfYieldConfig,
    "fundamentals": FundamentalsConfig,
}

_LIST_GROUP = "cyclical_sectors"
_LIST_KEY = "sector_codes"
_TOP_LEVEL = set(_SCHEMA) | {_LIST_GROUP}


def _require_mapping(value, what):
    if not isinstance(value, dict):
        raise Stage2ConfigError(f"{what} must be a mapping, got {type(value).__name__}")
    return value


def _validate_number(group, key, value):
    dotted = f"{group}.{key}"
    # bool is an int subclass and PyYAML resolves yes/no/true/false to bool.
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise Stage2ConfigError(f"{dotted} must be a number, got {value!r} ({type(value).__name__})")
    if (group, key) in _INTEGER_KEYS and not isinstance(value, int):
        raise Stage2ConfigError(f"{dotted} must be an integer, got {value!r} ({type(value).__name__})")
    low, high = _SCHEMA[group][key]
    if not low <= value <= high:
        raise Stage2ConfigError(f"{dotted} ({value}) must be between {low} and {high}")
    return int(value) if (group, key) in _INTEGER_KEYS else float(value)


def _validate_sector_codes(value):
    dotted = f"{_LIST_GROUP}.{_LIST_KEY}"
    if not isinstance(value, list):
        raise Stage2ConfigError(f"{dotted} must be a list of integers, got {value!r} ({type(value).__name__})")
    codes = []
    for code in value:
        if not isinstance(code, int) or isinstance(code, bool):
            raise Stage2ConfigError(f"{dotted} must contain only integers, got {code!r} ({type(code).__name__})")
        # Compared by parsed value, not by the written text: 107002 and 107_002 are the same integer.
        if code in codes:
            raise Stage2ConfigError(f"{dotted} lists the sector code {code} more than once")
        codes.append(code)
    return tuple(codes)


def load_stage2_config(path: Path = DEFAULT_STAGE2_PATH) -> Stage2Config:
    """Load, validate and return config/stage2.yaml.

    Raises Stage2ConfigError - naming the exact dotted key - for a missing file, invalid YAML,
    a missing or unrecognized group or leaf key, a duplicate key, a non-numeric or non-integer
    value, a value outside its plausibility range, a threshold pair on the wrong side of its
    partner, or a malformed cyclical sector list. There are no defaults anywhere in this
    function: every key is required.
    """
    path = Path(path)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Stage2ConfigError(f"could not read {path}: {exc}") from exc

    try:
        data = _parse_yaml(raw_text, path)
    except ThresholdsConfigError as exc:  # duplicate key, reported by the shared parser
        raise Stage2ConfigError(str(exc)) from exc
    except yaml.YAMLError as exc:
        raise Stage2ConfigError(f"{path} is not valid YAML: {exc}") from exc

    data = _require_mapping(data, str(path))

    unknown = sorted(set(data) - _TOP_LEVEL)
    if unknown:
        raise Stage2ConfigError(f"{path} has unrecognized top-level key(s): {unknown}")
    missing = sorted(_TOP_LEVEL - set(data))
    if missing:
        raise Stage2ConfigError(f"{path} is missing required key(s): {missing}")

    groups = {}
    for group, leaf_schema in _SCHEMA.items():
        group_data = _require_mapping(data[group], f"{path}: {group!r}")
        unknown_keys = sorted(set(group_data) - set(leaf_schema))
        if unknown_keys:
            raise Stage2ConfigError(
                f"{path} has unrecognized key(s) in {group!r}: {[f'{group}.{k}' for k in unknown_keys]}"
            )
        missing_keys = sorted(set(leaf_schema) - set(group_data))
        if missing_keys:
            raise Stage2ConfigError(f"{path} is missing required key(s): {[f'{group}.{k}' for k in missing_keys]}")
        validated = {key: _validate_number(group, key, group_data[key]) for key in leaf_schema}
        groups[group] = _GROUP_DATACLASSES[group](**validated)

    cyclical = _require_mapping(data[_LIST_GROUP], f"{path}: {_LIST_GROUP!r}")
    unknown_keys = sorted(set(cyclical) - {_LIST_KEY})
    if unknown_keys:
        raise Stage2ConfigError(
            f"{path} has unrecognized key(s) in {_LIST_GROUP!r}: {[f'{_LIST_GROUP}.{k}' for k in unknown_keys]}"
        )
    if _LIST_KEY not in cyclical:
        raise Stage2ConfigError(f"{path} is missing required key(s): ['{_LIST_GROUP}.{_LIST_KEY}']")
    groups[_LIST_GROUP] = CyclicalSectorsConfig(sector_codes=_validate_sector_codes(cyclical[_LIST_KEY]))

    return Stage2Config(**groups)


def compute_stage2_config_hash(path: Path = DEFAULT_STAGE2_PATH) -> str:
    """SHA-256 hex digest of config/stage2.yaml after normalizing line endings, so a Windows
    and a Unix checkout of the same content hash alike (same method as Stage 1)."""
    return compute_config_hash(path)
