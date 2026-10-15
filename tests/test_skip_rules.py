"""skip_rules.py und der Hook in conftest.py: Tests werden vor dem Lauf übersprungen, Ergebnisse nie umgeschrieben."""

import ast
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import conftest
import skip_rules as rules

RAW_MODULE = "test_run_stage2.py"
HOOK_TEST = "test_registered_hook_command_blocks_in_every_permission_mode"
SYMLINK_MODULE, SYMLINK_TEST = sorted(rules.SYMLINK_TESTS)[0]
OWN_FILES = {"conftest.py", "skip_rules.py", "test_skip_rules.py"}


def reason(module, test, raw=True, platform="linux", symlinks=True):
    return rules.skip_reason(module, test, raw_data=raw, platform=platform, symlinks_ok=lambda: symlinks)


def test_raw_data_modules_are_skipped_only_when_the_raw_data_is_missing():
    assert reason(RAW_MODULE, "test_x", raw=False) == rules.REASON_RAW
    assert reason(RAW_MODULE, "test_x", raw=True) is None
    assert reason("test_roic.py", "test_x", raw=False) is None


def test_every_module_in_the_list_is_skipped_without_the_data():
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

    assert rules.skip_reason("test_roic.py", "test_x", raw_data=True, platform="linux", symlinks_ok=boom) is None


@pytest.mark.skipif(os.name != "posix", reason="Linux and macOS can create symbolic links without a privilege")
def test_the_symlink_probe_succeeds_where_links_are_always_allowed():
    """Eine Probe, die immer False liefert, würde die drei Symlink-Tests hier still abschalten."""
    assert rules.symlinks_allowed() is True


@pytest.mark.parametrize("error", [OSError("WinError 1314"), NotImplementedError("no symlinks")])
def test_the_symlink_probe_returns_false_when_creating_a_link_fails(monkeypatch, error):
    def refuse(self, target, target_is_directory=False):
        raise error

    monkeypatch.setattr(Path, "symlink_to", refuse)
    assert rules.symlinks_allowed() is False


def test_the_raw_data_counts_as_present_only_with_at_least_one_json_file(monkeypatch, tmp_path):
    folder = tmp_path / "simfin_raw"
    monkeypatch.setattr(rules, "SIMFIN_RAW_DIR", folder)
    assert rules.raw_data_present() is False  # Ordner fehlt
    folder.mkdir()
    assert rules.raw_data_present() is False  # leer
    (folder / ".gitkeep").write_text("", encoding="utf-8")
    (folder / "README.md").write_text("x", encoding="utf-8")
    assert rules.raw_data_present() is False  # Platzhalterdateien sind keine Daten
    (folder / "AAA.json").write_text("{}", encoding="utf-8")
    assert rules.raw_data_present() is True


def test_the_path_of_the_raw_folder_is_the_one_the_test_modules_use():
    """Ein falscher Pfad in skip_rules würde im vollen Checkout 223 Tests still überspringen."""
    assert rules.TESTS_DIR == Path(__file__).parent
    assert rules.SIMFIN_RAW_DIR == Path(__file__).parent / "fixtures" / "simfin_raw"


def test_the_raw_folder_of_every_test_module_is_the_one_skip_rules_checks():
    """Die Testmodule lesen den Rohdaten-Ordner über ihre eigene Konstante SIMFIN_RAW_DIR. Zeigt eine davon woandershin
    als skip_rules, prüft skip_rules den falschen Ordner: im öffentlichen Repo wären die Tests rot statt übersprungen,
    im vollen Checkout liefen sie still nicht mehr (die Regeln selbst blieben grün)."""
    owners = {}
    for name in sorted(rules.RAW_DATA_MODULES):
        module = importlib.import_module(Path(name).stem)
        if hasattr(module, "SIMFIN_RAW_DIR"):
            owners[name] = Path(module.SIMFIN_RAW_DIR).resolve()
    assert owners, "kein Testmodul der Liste definiert SIMFIN_RAW_DIR mehr; der Vergleich prüft nichts"
    assert owners == {name: rules.SIMFIN_RAW_DIR.resolve() for name in owners}


def source_files():
    return {p.name: p for p in sorted(rules.TESTS_DIR.glob("*.py")) if p.name not in OWN_FILES}


def imported_modules(path):
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
    return found


def test_the_list_of_raw_data_modules_is_exactly_the_modules_that_use_the_raw_folder():
    """Neue Tests, die den Rohdaten-Ordner brauchen (direkt oder über ein Modul der Liste), müssen in
    RAW_DATA_MODULES stehen; sonst würden sie im öffentlichen Repo rot statt übersprungen. Umgekehrt darf kein
    Modul in der Liste stehen, das den Ordner nicht braucht (sonst liefe sein Test still nicht)."""
    files = source_files()
    needs = {name for name, path in files.items() if "SIMFIN_RAW_DIR" in path.read_text(encoding="utf-8")}
    while True:
        stems = {Path(name).stem for name in needs}
        more = {name for name, path in files.items() if name not in needs and imported_modules(path) & stems}
        if not more:
            break
        needs |= more
    assert needs == set(rules.RAW_DATA_MODULES)


def test_the_list_of_symlink_tests_is_exactly_the_tests_that_create_a_link():
    found = set()
    for name, path in source_files().items():
        for function in (n for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.FunctionDef)):
            if any(isinstance(call.func, ast.Attribute) and call.func.attr in ("symlink_to", "symlink")
                   for call in ast.walk(function) if isinstance(call, ast.Call)):
                found.add((name, function.name))
    assert found == set(rules.SYMLINK_TESTS)


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
    return SimpleNamespace(path=rules.TESTS_DIR / module, name=name + "[param]", originalname=name,
                           add_marker=marks.append, marks=marks)


def test_the_collection_hook_adds_a_skip_marker_with_the_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(rules, "SIMFIN_RAW_DIR", tmp_path / "missing")
    raw, other = fake_item(RAW_MODULE, "test_a"), fake_item("test_roic.py", "test_b")
    conftest.pytest_collection_modifyitems(None, [raw, other])
    assert len(raw.marks) == 1 and raw.marks[0].name == "skip" and raw.marks[0].kwargs["reason"] == rules.REASON_RAW
    assert other.marks == []


def test_the_collection_hook_adds_nothing_when_the_raw_data_exists(monkeypatch, tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "AAA.json").write_text("{}", encoding="utf-8")
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
