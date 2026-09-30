"""Business-model profile loading/validation per docs/decisions.md ("Phase
2.0e", Issue #51).

config/sector_profiles.yaml assigns every SimFin sectorCode to a profile (the
standard `non_financial`, or an excluded one such as `banks` with its reason) -
same contract as config/thresholds.yaml: every key is required, no code-side
defaults, and any problem aborts loading with a message naming the offending
key. A sectorCode that is not listed is *unknown*: assignment() returns None
for it, never a default profile - what to do about it (abort the run) is the
runner's decision, not this module's.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SECTOR_PROFILES_PATH = _REPO_ROOT / "config" / "sector_profiles.yaml"

_TOP_LEVEL_KEYS = ("profiles", "sectors")
_PROFILE_KEYS = ("supported", "reason")
_SECTOR_KEYS = ("profile", "borderline")


class SectorProfilesConfigError(Exception):
    """Raised when config/sector_profiles.yaml is missing, malformed, or fails
    validation. Every message names the offending key - no default is ever
    substituted for it."""


@dataclass(frozen=True)
class Profile:
    name: str
    supported: bool
    reason: Optional[str]  # required for an unsupported profile, None for a supported one


@dataclass(frozen=True)
class SectorAssignment:
    sector_code: int
    profile: Profile
    borderline: bool  # informational marker, only ever True on a supported profile


@dataclass(frozen=True)
class SectorProfiles:
    profiles: Dict[str, Profile]
    sectors: Dict[int, SectorAssignment]

    def assignment(self, sector_code) -> Optional[SectorAssignment]:
        """The assignment for sector_code, or None if it is not listed (or is None) - never a default."""
        if sector_code is None:
            return None
        return self.sectors.get(sector_code)

    def find_uncovered(self, sector_codes: Iterable) -> Tuple[List[int], int]:
        """(sorted distinct codes that are not listed, number of None codes) among sector_codes."""
        unknown = set()
        null_count = 0
        for code in sector_codes:
            if code is None:
                null_count += 1
            elif code not in self.sectors:
                unknown.add(code)
        return sorted(unknown), null_count


def _key_identity(loader, key_node):
    """The key as YAML will construct it, so that 104002 and 104_002 (or 0x19632) are the same key.

    Falls back to the raw (tag, text) pair if the key cannot be constructed or is not hashable.
    """
    try:
        value = loader.construct_object(key_node, deep=True)
        hash(value)
        return value
    except Exception:  # noqa: BLE001 - any construction problem just means "compare the raw text"
        return (key_node.tag, key_node.value)


def _reject_duplicates_within(loader, mapping_node, prefix, path):
    seen = {}
    for key_node, _ in mapping_node.value:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        line = key_node.start_mark.line + 1
        identity = _key_identity(loader, key_node)
        if identity in seen:
            first_line, first_text = seen[identity]
            spelling = f" (written {first_text!r} and {key_node.value!r})" if first_text != key_node.value else ""
            raise SectorProfilesConfigError(
                f"{path} has duplicate key {prefix + str(identity)!r}{spelling} (lines {first_line} and {line})"
            )
        seen[identity] = (line, key_node.value)


def _reject_duplicate_keys(loader, root_node, path):
    """Reject a key that appears twice in the same mapping at any of the schema's
    levels (top level, profile/sector names, their attributes): yaml.safe_load
    silently keeps the last of two equal keys, so a second `104002:` line would
    override the first without any error. Keys are compared by their PARSED value,
    not their spelling (104002 == 104_002 == 0x19632)."""
    if not isinstance(root_node, yaml.MappingNode):
        return
    _reject_duplicates_within(loader, root_node, "", path)
    for key_node, value_node in root_node.value:
        if not (isinstance(key_node, yaml.ScalarNode) and isinstance(value_node, yaml.MappingNode)):
            continue
        _reject_duplicates_within(loader, value_node, f"{key_node.value}.", path)
        for inner_key, inner_value in value_node.value:
            if isinstance(inner_key, yaml.ScalarNode) and isinstance(inner_value, yaml.MappingNode):
                _reject_duplicates_within(loader, inner_value, f"{key_node.value}.{inner_key.value}.", path)


def _parse_yaml(raw_text, path):
    """yaml.safe_load(raw_text), except that a duplicate key is rejected."""
    loader = yaml.SafeLoader(raw_text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        _reject_duplicate_keys(loader, node, path)
        return loader.construct_document(node)
    finally:
        loader.dispose()


def _require_mapping(value, what):
    if not isinstance(value, dict):
        raise SectorProfilesConfigError(f"{what} must be a mapping, got {type(value).__name__}")
    return value


def _check_keys(mapping, allowed, required, what, prefix=""):
    """Reject unrecognized and missing keys, naming each as a dotted key (prefix + key)."""
    unknown = sorted(f"{prefix}{key}" for key in set(mapping) - set(allowed))
    if unknown:
        raise SectorProfilesConfigError(f"{what} has unrecognized key(s): {unknown}")
    missing = [f"{prefix}{key}" for key in required if key not in mapping]
    if missing:
        raise SectorProfilesConfigError(f"{what} is missing required key(s): {missing}")


def _parse_profile(name, data, path):
    if not isinstance(name, str) or not name:
        raise SectorProfilesConfigError(f"{path}: profile name {name!r} must be a non-empty string")
    data = _require_mapping(data, f"{path}: profiles.{name}")
    _check_keys(data, _PROFILE_KEYS, ("supported",), f"{path}", f"profiles.{name}.")
    supported = data["supported"]
    if not isinstance(supported, bool):
        raise SectorProfilesConfigError(
            f"{path}: profiles.{name}.supported must be true or false, got {supported!r} ({type(supported).__name__})"
        )
    if supported:
        if "reason" in data:
            raise SectorProfilesConfigError(f"{path}: profiles.{name}.reason must be absent for a supported profile")
        return Profile(name=name, supported=True, reason=None)
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise SectorProfilesConfigError(
            f"{path}: profiles.{name}.reason is required (a non-empty string) for an unsupported profile, got {reason!r}"
        )
    return Profile(name=name, supported=False, reason=reason)


def _parse_sector(code, data, profiles, path):
    if not isinstance(code, int) or isinstance(code, bool):
        raise SectorProfilesConfigError(f"{path}: sector code {code!r} must be an integer ({type(code).__name__})")
    data = _require_mapping(data, f"{path}: sectors.{code}")
    _check_keys(data, _SECTOR_KEYS, _SECTOR_KEYS, f"{path}", f"sectors.{code}.")
    profile_name = data["profile"]
    if not isinstance(profile_name, str):
        raise SectorProfilesConfigError(
            f"{path}: sectors.{code}.profile must be a profile name (a string), got {profile_name!r} ({type(profile_name).__name__})"
        )
    if profile_name not in profiles:
        raise SectorProfilesConfigError(
            f"{path}: sectors.{code}.profile {profile_name!r} is not a defined profile ({sorted(profiles)})"
        )
    borderline = data["borderline"]
    if not isinstance(borderline, bool):
        raise SectorProfilesConfigError(
            f"{path}: sectors.{code}.borderline must be true or false, got {borderline!r} ({type(borderline).__name__})"
        )
    profile = profiles[profile_name]
    if borderline and not profile.supported:
        raise SectorProfilesConfigError(
            f"{path}: sectors.{code}.borderline must be false on the unsupported profile {profile_name!r}"
        )
    return SectorAssignment(sector_code=code, profile=profile, borderline=borderline)


def load_sector_profiles(path: Path = DEFAULT_SECTOR_PROFILES_PATH) -> SectorProfiles:
    """Load, validate and return config/sector_profiles.yaml.

    Raises SectorProfilesConfigError - naming the offending key - for a missing
    or unreadable file, invalid YAML, a missing or unrecognized top-level key,
    an empty `profiles` or `sectors` group, a duplicate profile name or sector
    code, a profile without a proper `supported` flag (or with a missing/extra
    `reason`), a sector code that is not an integer, a sector whose profile is
    not defined or whose `borderline` is missing, not a bool, or set on an
    unsupported profile. There are no defaults: every key is required.
    """
    path = Path(path)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SectorProfilesConfigError(f"could not read {path}: {exc}") from exc
    try:
        data = _parse_yaml(raw_text, path)
    except yaml.YAMLError as exc:
        raise SectorProfilesConfigError(f"{path} is not valid YAML: {exc}") from exc

    data = _require_mapping(data, str(path))
    _check_keys(data, _TOP_LEVEL_KEYS, _TOP_LEVEL_KEYS, str(path))

    raw_profiles = _require_mapping(data["profiles"], f"{path}: 'profiles'")
    if not raw_profiles:
        raise SectorProfilesConfigError(f"{path}: 'profiles' must define at least one profile")
    profiles = {name: _parse_profile(name, value, path) for name, value in raw_profiles.items()}

    raw_sectors = _require_mapping(data["sectors"], f"{path}: 'sectors'")
    if not raw_sectors:
        raise SectorProfilesConfigError(f"{path}: 'sectors' must list at least one sector code")
    sectors = {code: _parse_sector(code, value, profiles, path) for code, value in raw_sectors.items()}

    return SectorProfiles(profiles=profiles, sectors=sectors)
