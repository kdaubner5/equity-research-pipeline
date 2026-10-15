"""Which tests are skipped (at collection time) when this checkout lacks something they need.

Pure selection logic, no pytest hooks: tests/conftest.py applies it, tests/test_skip_rules.py tests it.
Nothing here rewrites a result. A test either runs and reports what it finds, or it is skipped before it runs,
with the reason shown in the skip summary (pytest -rs).

Three cases:

1. Licensed SimFin sample data (tests/fixtures/simfin_raw/) is not redistributed (SimFin free plan: personal research
   use only), so the public repository has no such folder. The modules that build their inputs from it are skipped as
   a whole when the folder is missing or holds no *.json file (a placeholder file does not count as data). In the
   full checkout the folder exists and nothing is skipped.
2. The registered Bash-guard hook is run through POSIX `sh -c` (tests/test_bash_guard.py, run_registered_hook). That
   does not work in the Windows setup used so far (known, not fixed yet): skipped on Windows only.
3. Creating symbolic links needs a privilege on Windows (WinError 1314): the tests that create one are skipped when a
   probe cannot create a link.
"""
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).parent
SIMFIN_RAW_DIR = TESTS_DIR / "fixtures" / "simfin_raw"

# Test modules that build their inputs from the SimFin raw files (checked by tests/test_skip_rules.py).
RAW_DATA_MODULES = frozenset({
    "test_run_stage1.py",
    "test_run_stage1_full.py",
    "test_run_stage1_full_profiles.py",
    "test_run_stage2.py",
    "test_run_stage2_plausibility.py",
    "test_run_trend_guard.py",
    "test_simfin_client.py",
    "test_simfin_pilot_gates.py",
})

HOOK_MODULE = "test_bash_guard.py"
HOOK_TEST_PREFIX = "test_registered_hook_command"

# (module, test function) pairs that create symbolic links.
SYMLINK_TESTS = frozenset({
    ("test_check_archive_schema.py", "test_a_link_to_the_real_archive_is_refused_too"),
    ("test_run_export_candidates.py", "test_a_link_to_the_real_archive_is_refused"),
    ("test_sensitivity_stage1.py", "test_cli_refuses_links_and_side_files"),
})

REASON_RAW = "needs the licensed SimFin raw data (tests/fixtures/simfin_raw), which is not part of this checkout"
REASON_HOOK = "runs the registered hook through POSIX `sh -c`, which does not work on Windows (known, not fixed yet)"
REASON_SYMLINK = "creating symbolic links is not permitted here (Windows: WinError 1314)"


def raw_data_present():
    """True if tests/fixtures/simfin_raw/ exists and holds at least one *.json file."""
    return SIMFIN_RAW_DIR.is_dir() and any(SIMFIN_RAW_DIR.glob("*.json"))


def symlinks_allowed():
    """True if this process can create a symbolic link (probe in a temporary directory)."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "target"
        target.write_text("x", encoding="utf-8")
        try:
            (Path(tmp) / "link").symlink_to(target)
        except (OSError, NotImplementedError):
            return False
    return True


def skip_reason(module, test, *, raw_data, platform, symlinks_ok=symlinks_allowed):
    """Reason to skip the test `test` (function name without parameters) in file `module`, or None to run it.

    `symlinks_ok` is called only for the symlink tests, so the probe costs nothing in other runs.
    """
    if module in RAW_DATA_MODULES and not raw_data:
        return REASON_RAW
    if module == HOOK_MODULE and test.startswith(HOOK_TEST_PREFIX) and platform == "win32":
        return REASON_HOOK
    if (module, test) in SYMLINK_TESTS and not symlinks_ok():
        return REASON_SYMLINK
    return None
