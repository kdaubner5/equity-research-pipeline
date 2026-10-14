import io
import json
import sys
from pathlib import Path

import pytest

import skip_rules
from config_layer.thresholds import load_thresholds

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "companies"

# Loaded once from the real config/thresholds.yaml, not a re-hardcoded
# literal copy - keeps every test that needs thresholds honest against the
# actual production config instead of silently drifting from it after a
# future recalibration (docs/decisions.md, "Schwellen nach
# config/thresholds.yaml").
DEFAULT_THRESHOLDS = load_thresholds()


class FakeUrlopenResponse(io.BytesIO):
    """A urllib.request.urlopen() response stand-in for offline network mocks
    (test_simfin_client.py, test_run_stage1.py) - a context-managed,
    read()-able byte stream is all json.load(resp) needs."""


def load_company(company_id):
    """Load a company fixture by id, periods sorted by fiscal_year."""
    with open(FIXTURES_DIR / f"{company_id}.json") as f:
        data = json.load(f)
    data["periods"] = sorted(data["periods"], key=lambda p: p["fiscal_year"])
    return data


def all_company_ids():
    """All fixture company_ids, sorted by ticker."""
    return sorted(path.stem for path in FIXTURES_DIR.glob("*.json"))


def pytest_collection_modifyitems(config, items):
    """Skip, before they run, the tests whose preconditions this checkout lacks (rules and reasons: skip_rules.py).
    Results are never rewritten: a test that runs reports what it finds."""
    raw_dir_exists = skip_rules.SIMFIN_RAW_DIR.is_dir()
    for item in items:
        module = item.path.name
        test = getattr(item, "originalname", None) or item.name
        reason = skip_rules.skip_reason(module, test, raw_dir_exists=raw_dir_exists, platform=sys.platform)
        if reason:
            item.add_marker(pytest.mark.skip(reason=reason))
