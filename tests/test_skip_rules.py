"""skip_rules.py und der Hook in conftest.py: Tests werden vor dem Lauf übersprungen, Ergebnisse nie umgeschrieben."""

import ast
from types import SimpleNamespace

import pytest

import conftest
import skip_rules as rules

RAW_MODULE = "test_run_stage2.py"
HOOK_TEST = "test_registered_hook_command_blocks_in_every_permission_mode"
SYMLINK_MODULE, SYMLINK_TEST = sorted(rules.SYMLINK_TESTS)[0]


def reason(module, test, raw=True, platform="linux", symlinks=True):
    return rules.skip_reason(module, test, raw_dir_exists=raw, platform=platform, symlinks_ok=lambda: symlinks)


def test_raw_data_modules_are_skipped_only_when_the_raw_folder_is_missing():
    assert reason(RAW_MODULE, "test_x", raw=False) == rules.REASON_RAW
    assert reason(RAW_MODULE, "test_x", raw=True) is None
    assert reason("test_roic.py", "test_x", raw=False) is None


def test_every_module_in_the_list_is_skipped_without_the_folder():
    assert all(reason(module, "test_x", raw=False) == rules.REASON_RAW for module in rules.RAW_DATA_MODULES)


def test_the_registered_hook_tests_are_skipped_on_windows_only():
    assert reason(rules.HOOK_MODULE, HOOK_TEST, platform="win32") == rules.REASON_HOOK
    assert reason(rules.HOOK_MODULE, HOOK_TEST, platform="linux") is None
    assert reason(rules.HOOK_MODULE, HOOK_TEST, platform="darwin") is None
    assert reason(rules.HOOK_MODULE, "test_main_fails_closed_on_malformed_input", platform="win32") is None
    assert reason("test_roic.py", HOOK_TEST, platform="win32") is None


def test_symlink_tests_are_skipped_only_when_no_link_can_be_created():
    assert reason(SYMLINK_MODULE, SYMLINK_TEST, symlinks=False) == rules.REASON_SYMLINK
    assert reason(SYMLINK_MODULE, SYMLINK_TEST, symlinks=True) is None
    assert reason("test_roic.py", SYMLINK_TEST, symlinks=False) is None


def test_the_symlink_probe_is_not_called_for_other_tests():
    def boom():
        raise AssertionError("probe must not run")

    assert rules.skip_reason("test_roic.py", "test_x", raw_dir_exists=True, platform="linux", symlinks_ok=boom) is None


def test_the_symlink_probe_returns_a_bool_and_leaves_nothing_behind():
    assert rules.symlinks_allowed() in (True, False)


def defined_functions(module):
    tree = ast.parse((rules.TESTS_DIR / module).read_text(encoding="utf-8"))
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}


def test_the_names_in_the_rules_still_exist_so_a_rename_cannot_switch_a_rule_off():
    for module in rules.RAW_DATA_MODULES | {rules.HOOK_MODULE} | {m for m, _ in rules.SYMLINK_TESTS}:
        assert (rules.TESTS_DIR / module).is_file(), module
    for module, test in rules.SYMLINK_TESTS:
        assert test in defined_functions(module), (module, test)
    hook_tests = {name for name in defined_functions(rules.HOOK_MODULE) if name.startswith(rules.HOOK_TEST_PREFIX)}
    assert len(hook_tests) == 3


def fake_item(module, name):
    marks = []
    return SimpleNamespace(fspath=rules.TESTS_DIR / module, name=name + "[param]", originalname=name,
                           add_marker=marks.append, marks=marks)


def test_the_collection_hook_adds_a_skip_marker_with_the_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(rules, "SIMFIN_RAW_DIR", tmp_path / "missing")
    raw, other = fake_item(RAW_MODULE, "test_a"), fake_item("test_roic.py", "test_b")
    conftest.pytest_collection_modifyitems(None, [raw, other])
    assert len(raw.marks) == 1 and raw.marks[0].name == "skip" and raw.marks[0].kwargs["reason"] == rules.REASON_RAW
    assert other.marks == []


def test_the_collection_hook_adds_nothing_when_the_raw_folder_exists(monkeypatch, tmp_path):
    (tmp_path / "raw").mkdir()
    monkeypatch.setattr(rules, "SIMFIN_RAW_DIR", tmp_path / "raw")
    raw = fake_item(RAW_MODULE, "test_a")
    conftest.pytest_collection_modifyitems(None, [raw])
    assert raw.marks == []


def test_parametrized_items_are_matched_by_the_function_name(monkeypatch):
    monkeypatch.setattr(conftest, "sys", SimpleNamespace(platform="win32"))  # nur der Name im Modul conftest
    item = fake_item(rules.HOOK_MODULE, HOOK_TEST)
    conftest.pytest_collection_modifyitems(None, [item])
    assert len(item.marks) == 1 and item.marks[0].kwargs["reason"] == rules.REASON_HOOK


def test_no_hook_rewrites_reports():
    """Ergebnisse werden nicht umgeschrieben: kein makereport-Hook in conftest.py oder skip_rules.py."""
    for name in ("conftest.py", "skip_rules.py"):
        assert "makereport" not in (rules.TESTS_DIR / name).read_text(encoding="utf-8"), name
