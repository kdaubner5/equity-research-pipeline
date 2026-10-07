"""Trend-Guard configuration loading/validation, per docs/decisions.md ("Trend-Guard (Masterplan
Stufe 1.1, Hebel 3 aus Issue #52)", Abschnitt 4).

Every parameter lives in config/trend_guard.yaml, never as a Python constant with a code-side
default - a missing, unrecognized or duplicate key aborts loading with the exact dotted key name
(CLAUDE.md: "keine Abkuerzungen bei Datenintegritaet"). The file is separate from
config/thresholds.yaml on purpose: Stage 1 and its config_hash stay untouched. Signal T3 (ROIC of the
last year) has no threshold of its own: it reads roic.fail_below from the Stage-1 thresholds.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from config_layer.thresholds import ThresholdsConfigError, _parse_yaml, compute_config_hash

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_TREND_GUARD_PATH = _REPO_ROOT / "config" / "trend_guard.yaml"

_GROUP = "trend_guard"


class TrendGuardConfigError(Exception):
    """Raised when config/trend_guard.yaml is missing, malformed, or fails validation.
    Every message names the offending dotted key - no default is ever substituted."""


@dataclass(frozen=True)
class TrendGuardConfig:
    years: int
    min_years: int
    revenue_ratio_flag_below: float
    ebitda_margin_ratio_flag_below: float
    ebitda_margin_ratio_flag_above: float

    def __post_init__(self):
        if not self.min_years <= self.years:
            raise TrendGuardConfigError(
                f"trend_guard.min_years ({self.min_years}) must not exceed trend_guard.years ({self.years})"
            )
        for key in ("revenue_ratio_flag_below", "ebitda_margin_ratio_flag_below"):
            value = getattr(self, key)
            if not 0 < value < 1:
                raise TrendGuardConfigError(f"trend_guard.{key} ({value}) must be greater than 0 and less than 1")
        if not self.ebitda_margin_ratio_flag_above > 1:
            raise TrendGuardConfigError(
                f"trend_guard.ebitda_margin_ratio_flag_above ({self.ebitda_margin_ratio_flag_above}) must be greater than 1"
            )


# key -> (min, max), inclusive. Coarse plausibility guardrails against a data-entry slip
# (e.g. "125" instead of "1.25"), not a second copy of the business logic.
_SCHEMA = {
    "years": (3, 5),
    "min_years": (2, 5),
    "revenue_ratio_flag_below": (0.0, 1.0),
    "ebitda_margin_ratio_flag_below": (0.0, 1.0),
    "ebitda_margin_ratio_flag_above": (1.0, 10.0),
}
_INTEGER_KEYS = {"years", "min_years"}


def _validate_number(key, value):
    dotted = f"{_GROUP}.{key}"
    # bool is an int subclass and PyYAML resolves yes/no/true/false to bool.
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TrendGuardConfigError(f"{dotted} must be a number, got {value!r} ({type(value).__name__})")
    if key in _INTEGER_KEYS and not isinstance(value, int):
        raise TrendGuardConfigError(f"{dotted} must be an integer, got {value!r} ({type(value).__name__})")
    low, high = _SCHEMA[key]
    if not low <= value <= high:
        raise TrendGuardConfigError(f"{dotted} ({value}) must be between {low} and {high}")
    return int(value) if key in _INTEGER_KEYS else float(value)


def load_trend_guard_config(path: Path = DEFAULT_TREND_GUARD_PATH) -> TrendGuardConfig:
    """Load, validate and return config/trend_guard.yaml.

    Raises TrendGuardConfigError - naming the exact dotted key - for a missing file, invalid YAML,
    a missing or unrecognized group or key, a duplicate key, a non-numeric or non-integer value, a
    value outside its plausibility range or a threshold on the wrong side of 1. No defaults.
    """
    path = Path(path)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TrendGuardConfigError(f"could not read {path}: {exc}") from exc
    try:
        data = _parse_yaml(raw_text, path)
    except ThresholdsConfigError as exc:  # duplicate key, reported by the shared parser
        raise TrendGuardConfigError(str(exc)) from exc
    except yaml.YAMLError as exc:
        raise TrendGuardConfigError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise TrendGuardConfigError(f"{path} must be a mapping, got {type(data).__name__}")
    unknown = sorted(set(data) - {_GROUP})
    if unknown:
        raise TrendGuardConfigError(f"{path} has unrecognized top-level key(s): {unknown}")
    if _GROUP not in data:
        raise TrendGuardConfigError(f"{path} is missing required key(s): ['{_GROUP}']")
    group = data[_GROUP]
    if not isinstance(group, dict):
        raise TrendGuardConfigError(f"{path}: {_GROUP!r} must be a mapping, got {type(group).__name__}")
    unknown_keys = sorted(set(group) - set(_SCHEMA))
    if unknown_keys:
        raise TrendGuardConfigError(f"{path} has unrecognized key(s) in {_GROUP!r}: {[f'{_GROUP}.{k}' for k in unknown_keys]}")
    missing_keys = sorted(set(_SCHEMA) - set(group))
    if missing_keys:
        raise TrendGuardConfigError(f"{path} is missing required key(s): {[f'{_GROUP}.{k}' for k in missing_keys]}")
    return TrendGuardConfig(**{key: _validate_number(key, group[key]) for key in _SCHEMA})


def compute_trend_guard_config_hash(path: Path = DEFAULT_TREND_GUARD_PATH) -> str:
    """SHA-256 hex digest of config/trend_guard.yaml after normalizing line endings (same method as Stage 1)."""
    return compute_config_hash(path)
