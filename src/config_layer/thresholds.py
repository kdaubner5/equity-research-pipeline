"""Stage-1 threshold loading/validation, per docs/decisions.md ("Schwellen
nach config/thresholds.yaml", Issue #33).

Every numeric threshold used by the metrics_layer/gates_layer/data_layer
modules lives in config/thresholds.yaml, never as a Python constant with a
code-side default - a missing or unrecognized key aborts loading with the
exact dotted key name (CLAUDE.md: "keine Abkürzungen bei Datenintegrität").
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_THRESHOLDS_PATH = _REPO_ROOT / "config" / "thresholds.yaml"


class ThresholdsConfigError(Exception):
    """Raised when config/thresholds.yaml is missing, malformed, or fails
    validation. Every message names the exact offending dotted key (e.g.
    'roic.fail_below') - no default is ever substituted for it."""


@dataclass(frozen=True)
class RoicThresholds:
    nopat_tax_rate: float
    ic_asset_floor_ratio: float
    implausible_above: float
    clean_pass_above: float
    fail_below: float

    def __post_init__(self):
        if not self.clean_pass_above > self.fail_below:
            raise ThresholdsConfigError(
                f"roic.clean_pass_above ({self.clean_pass_above}) must be greater than "
                f"roic.fail_below ({self.fail_below})"
            )


@dataclass(frozen=True)
class NetDebtEbitdaThresholds:
    clean_pass_below: float
    fail_at_or_above: float

    def __post_init__(self):
        if not self.clean_pass_below < self.fail_at_or_above:
            raise ThresholdsConfigError(
                f"net_debt_ebitda.clean_pass_below ({self.clean_pass_below}) must be less than "
                f"net_debt_ebitda.fail_at_or_above ({self.fail_at_or_above})"
            )


@dataclass(frozen=True)
class CashConversionThresholds:
    clean_pass_at_or_above: float
    fail_below: float

    def __post_init__(self):
        if not self.clean_pass_at_or_above > self.fail_below:
            raise ThresholdsConfigError(
                f"cash_conversion.clean_pass_at_or_above ({self.clean_pass_at_or_above}) must be greater "
                f"than cash_conversion.fail_below ({self.fail_below})"
            )


@dataclass(frozen=True)
class NetIncomeMarginThresholds:
    # No grey zone (PR #27, docs/decisions.md "Net-Income-Margin-Gate") - a
    # single hard threshold, so no ordering invariant to check here.
    fail_below: float


@dataclass(frozen=True)
class DataQualityReconciliationThresholds:
    debt_tolerance_ratio: float
    debt_tolerance_assets_ratio: float


@dataclass(frozen=True)
class Thresholds:
    roic: RoicThresholds
    net_debt_ebitda: NetDebtEbitdaThresholds
    cash_conversion: CashConversionThresholds
    net_income_margin: NetIncomeMarginThresholds
    data_quality_reconciliation: DataQualityReconciliationThresholds


# (group, key) -> (dataclass field, (min, max)). Range bounds are a coarse
# plausibility guardrail against a data-entry slip (e.g. "12" instead of
# "0.12"), not a second copy of the grey-zone business logic. cash_conversion
# is deliberately NOT bounded at 1.0 - CFO/Net-Income can legitimately exceed
# it (masterplan.md §4 names 2.0 as a ranking cap for this very metric).
_SCHEMA = {
    "roic": {
        "nopat_tax_rate": (0.0, 1.0),
        "ic_asset_floor_ratio": (0.0, 1.0),
        "implausible_above": (0.0, 10.0),
        "clean_pass_above": (0.0, 10.0),
        "fail_below": (0.0, 10.0),
    },
    "net_debt_ebitda": {
        "clean_pass_below": (0.0, 50.0),
        "fail_at_or_above": (0.0, 50.0),
    },
    "cash_conversion": {
        "clean_pass_at_or_above": (0.0, 10.0),
        "fail_below": (0.0, 10.0),
    },
    "net_income_margin": {
        "fail_below": (-1.0, 1.0),
    },
    "data_quality_reconciliation": {
        "debt_tolerance_ratio": (0.0, 1.0),
        "debt_tolerance_assets_ratio": (0.0, 1.0),
    },
}

_GROUP_DATACLASSES = {
    "roic": RoicThresholds,
    "net_debt_ebitda": NetDebtEbitdaThresholds,
    "cash_conversion": CashConversionThresholds,
    "net_income_margin": NetIncomeMarginThresholds,
    "data_quality_reconciliation": DataQualityReconciliationThresholds,
}


def _require_mapping(value, what):
    if not isinstance(value, dict):
        raise ThresholdsConfigError(f"{what} must be a mapping, got {type(value).__name__}")
    return value


def _validate_number(group, key, value):
    dotted = f"{group}.{key}"
    # isinstance(True, int) is True in Python - PyYAML's safe_load resolves
    # YAML 1.1 yes/no/true/false/on/off to bool, so a fat-fingered
    # "clean_pass_above: yes" would otherwise silently become 1.0.
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ThresholdsConfigError(f"{dotted} must be a number, got {value!r} ({type(value).__name__})")
    low, high = _SCHEMA[group][key]
    if not low <= value <= high:
        raise ThresholdsConfigError(f"{dotted} ({value}) must be between {low} and {high}")
    return float(value)


def load_thresholds(path: Path = DEFAULT_THRESHOLDS_PATH) -> Thresholds:
    """Load, validate and return config/thresholds.yaml's Stage-1 thresholds.

    Raises ThresholdsConfigError - naming the exact dotted key - for a
    missing file, a missing or unrecognized top-level group or leaf key, a
    non-numeric value, a value outside its plausibility range, or a
    grey-zone pair on the wrong side of its hard threshold. There are no
    defaults anywhere in this function: every one of the twelve leaf keys is
    required.
    """
    path = Path(path)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ThresholdsConfigError(f"could not read {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ThresholdsConfigError(f"{path} is not valid YAML: {exc}") from exc

    data = _require_mapping(data, str(path))

    unknown_groups = sorted(set(data) - set(_SCHEMA))
    if unknown_groups:
        raise ThresholdsConfigError(f"{path} has unrecognized top-level key(s): {unknown_groups}")
    missing_groups = sorted(set(_SCHEMA) - set(data))
    if missing_groups:
        raise ThresholdsConfigError(f"{path} is missing required key(s): {missing_groups}")

    groups = {}
    for group, leaf_schema in _SCHEMA.items():
        group_data = _require_mapping(data[group], f"{path}: {group!r}")

        unknown_keys = sorted(set(group_data) - set(leaf_schema))
        if unknown_keys:
            raise ThresholdsConfigError(
                f"{path} has unrecognized key(s) in {group!r}: {[f'{group}.{k}' for k in unknown_keys]}"
            )
        missing_keys = sorted(set(leaf_schema) - set(group_data))
        if missing_keys:
            raise ThresholdsConfigError(
                f"{path} is missing required key(s): {[f'{group}.{k}' for k in missing_keys]}"
            )

        validated = {key: _validate_number(group, key, group_data[key]) for key in leaf_schema}
        groups[group] = _GROUP_DATACLASSES[group](**validated)

    return Thresholds(**groups)


def compute_config_hash(path: Path = DEFAULT_THRESHOLDS_PATH) -> str:
    """SHA-256 hex digest of config/thresholds.yaml, after normalizing line
    endings (\\r\\n -> \\n) - not the raw bytes.

    Without normalization, a Windows checkout of the exact same content as a
    Unix checkout would hash differently, even though no threshold changed.
    config/*.yaml is also pinned to LF via .gitattributes as the primary
    defense; this normalization is the backstop, not the only guard.
    """
    raw_bytes = Path(path).read_bytes()
    normalized = raw_bytes.replace(b"\r\n", b"\n")
    return hashlib.sha256(normalized).hexdigest()
