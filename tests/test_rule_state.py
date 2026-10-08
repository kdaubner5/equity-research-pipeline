"""Rule state per run (docs/decisions.md "Regelstand und Kurs je Lauf im Archiv", Punkt 8): git state clean/dirty/unknown,
rule-file content, the tables. Git cases run in a temporary repository (tmp_path); nothing touches the real archive."""

import sqlite3
import subprocess
from pathlib import Path

import pytest

from config_layer.thresholds import compute_config_hash
from store_layer.rule_state import (
    CODE_CLEAN,
    CODE_DIRTY,
    CODE_UNKNOWN,
    MAX_DIRTY_PATHS,
    NON_RULE_CONFIG_FILES,
    REPO_ROOT,
    RULE_FILES,
    RuleState,
    RuleStateError,
    capture_rule_state,
    init_rule_state_tables,
    write_rule_state,
)

GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", "-c", "commit.gpgsign=false"]


def git(root, *args):
    subprocess.run([*GIT, "-C", str(root), *args], check=True, capture_output=True)


def make_repo(tmp_path):
    """A repository with the four rule files, committed."""
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "src").mkdir()
    for index, relative in enumerate(RULE_FILES):
        (root / relative).write_text(f"key_{index}: {index}\n", encoding="utf-8", newline="\n")
    (root / "src" / "module.py").write_text("X = 1\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "config", "src")
    git(root, "commit", "-q", "-m", "init")
    return root


def head(root):
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


# --- git state -------------------------------------------------------------------------------------------------


def test_clean_after_commit_has_the_40_character_commit(tmp_path):
    root = make_repo(tmp_path)
    state = capture_rule_state(root)
    assert state.code_state == CODE_CLEAN
    assert state.git_commit == head(root) and len(state.git_commit) == 40
    assert state.dirty_paths == ()


def test_dirty_after_changing_a_tracked_file(tmp_path):
    root = make_repo(tmp_path)
    (root / "config" / "thresholds.yaml").write_text("changed: 1\n", encoding="utf-8")
    state = capture_rule_state(root)
    assert state.code_state == CODE_DIRTY
    assert state.dirty_paths == ("config/thresholds.yaml",)


def test_dirty_after_staging_a_change(tmp_path):
    root = make_repo(tmp_path)
    (root / "src" / "module.py").write_text("X = 2\n", encoding="utf-8")
    git(root, "add", "src/module.py")
    assert capture_rule_state(root).code_state == CODE_DIRTY


def test_dirty_after_deleting_a_tracked_file(tmp_path):
    root = make_repo(tmp_path)
    (root / "src" / "module.py").unlink()
    state = capture_rule_state(root)
    assert state.code_state == CODE_DIRTY and state.dirty_paths == ("src/module.py",)


def test_dirty_for_a_rename_lists_the_new_path_once(tmp_path):
    root = make_repo(tmp_path)
    git(root, "mv", "src/module.py", "src/other.py")
    state = capture_rule_state(root)
    assert state.code_state == CODE_DIRTY
    assert state.dirty_paths == ("src/other.py",)


@pytest.mark.parametrize("relative", ["config/new_rules.yaml", "src/new_module.py", "run_new.py"])
def test_dirty_for_a_new_untracked_file_in_config_src_or_run_script(tmp_path, relative):
    root = make_repo(tmp_path)
    (root / relative).write_text("x\n", encoding="utf-8")
    state = capture_rule_state(root)
    assert state.code_state == CODE_DIRTY
    assert state.dirty_paths == (relative,)


@pytest.mark.parametrize("relative", ["check_tickers.py", "notes.txt", "tests/test_new.py", "docs/new.md"])
def test_clean_for_other_untracked_files(tmp_path, relative):
    root = make_repo(tmp_path)
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x\n", encoding="utf-8")
    assert capture_rule_state(root).code_state == CODE_CLEAN


def test_ignored_files_do_not_make_it_dirty(tmp_path):
    root = make_repo(tmp_path)
    (root / ".gitignore").write_text("data/\n", encoding="utf-8")
    git(root, "add", ".gitignore")
    git(root, "commit", "-q", "-m", "ignore")
    (root / "data").mkdir()
    (root / "data" / "cache.json").write_text("{}", encoding="utf-8")
    assert capture_rule_state(root).code_state == CODE_CLEAN


def test_dirty_paths_are_cut_at_the_limit_when_stored(tmp_path):
    root = make_repo(tmp_path)
    for index in range(MAX_DIRTY_PATHS + 7):
        (root / "src" / f"extra_{index:03d}.py").write_text("x\n", encoding="utf-8")
    state = capture_rule_state(root)
    assert len(state.dirty_paths) == MAX_DIRTY_PATHS + 7  # the state keeps all of them
    connection = sqlite3.connect(":memory:")
    write_rule_state(connection, "stage1", "2026-10-08T10:00:00", state)
    stored = connection.execute("SELECT dirty_paths FROM run_rule_state").fetchone()[0].split("\n")
    assert len(stored) == MAX_DIRTY_PATHS + 1
    assert stored[-1] == "(und 7 weitere)"


def test_no_repository_means_unknown_with_no_commit(tmp_path):
    root = tmp_path / "plain"
    (root / "config").mkdir(parents=True)
    for relative in RULE_FILES:
        (root / relative).write_text("a: 1\n", encoding="utf-8")
    # an unrelated parent repository must not be picked up: ask git about a directory outside any repository
    state = capture_rule_state(root, git_runner=lambda args: (128, b""))
    assert state.code_state == CODE_UNKNOWN and state.git_commit is None and state.dirty_paths == ()


def test_git_not_callable_means_unknown_never_clean(tmp_path):
    root = make_repo(tmp_path)

    def broken(args):
        return 127, b""

    state = capture_rule_state(root, git_runner=broken)
    assert state.code_state == CODE_UNKNOWN and state.git_commit is None


def test_rev_parse_ok_but_status_fails_keeps_the_commit_and_is_unknown(tmp_path):
    root = make_repo(tmp_path)
    commit = head(root)

    def runner(args):
        return (0, (commit + "\n").encode()) if args[0] == "rev-parse" else (1, b"")

    state = capture_rule_state(root, git_runner=runner)
    assert state.code_state == CODE_UNKNOWN and state.git_commit == commit


def test_garbage_from_rev_parse_is_unknown(tmp_path):
    root = make_repo(tmp_path)
    state = capture_rule_state(root, git_runner=lambda args: (0, b"not a commit\n"))
    assert state.code_state == CODE_UNKNOWN and state.git_commit is None


# --- rule files ------------------------------------------------------------------------------------------------


def test_files_are_stored_with_content_in_rule_file_order(tmp_path):
    root = make_repo(tmp_path)
    state = capture_rule_state(root)
    assert [f.path for f in state.files] == list(RULE_FILES)
    assert state.files[0].content == "key_0: 0\n"


def test_crlf_and_lf_give_the_same_sha256_and_content(tmp_path):
    root = make_repo(tmp_path)
    lf = capture_rule_state(root).files[0]
    (root / RULE_FILES[0]).write_bytes(b"key_0: 0\r\n")
    crlf = capture_rule_state(root).files[0]
    assert crlf.sha256 == lf.sha256 and crlf.content == lf.content == "key_0: 0\n"


def test_stored_bytes_are_the_bytes_compute_config_hash_hashes(tmp_path):
    root = make_repo(tmp_path)
    (root / RULE_FILES[0]).write_bytes(b"a: 1\r\nb: 2\r\n")
    state = capture_rule_state(root)
    assert state.files[0].sha256 == compute_config_hash(root / RULE_FILES[0])


def test_a_missing_rule_file_stops_with_an_error(tmp_path):
    root = make_repo(tmp_path)
    (root / RULE_FILES[2]).unlink()
    with pytest.raises(RuleStateError, match="trend_guard.yaml"):
        capture_rule_state(root)


def test_an_unreadable_rule_file_stops_with_an_error(tmp_path):
    root = make_repo(tmp_path)
    (root / RULE_FILES[1]).write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(RuleStateError, match="UTF-8"):
        capture_rule_state(root)


def test_rule_paths_override_the_default_location(tmp_path):
    root = make_repo(tmp_path)
    other = tmp_path / "custom_thresholds.yaml"
    other.write_text("custom: true\n", encoding="utf-8")
    state = capture_rule_state(root, rule_paths={"config/thresholds.yaml": other})
    assert state.files[0].content == "custom: true\n"


def test_rule_paths_with_an_unknown_name_are_refused(tmp_path):
    root = make_repo(tmp_path)
    with pytest.raises(ValueError):
        capture_rule_state(root, rule_paths={"config/bash_guard.yaml": root / "config" / "x"})


def test_the_rule_file_list_matches_the_real_config_directory():
    """A new file in config/ must be added to RULE_FILES or to NON_RULE_CONFIG_FILES - never forgotten silently."""
    on_disk = {f"config/{path.name}" for path in (REPO_ROOT / "config").iterdir() if path.is_file()}
    assert on_disk == set(RULE_FILES) | set(NON_RULE_CONFIG_FILES)
    assert not set(RULE_FILES) & set(NON_RULE_CONFIG_FILES)


def test_the_real_repository_can_be_captured_and_thresholds_match_the_existing_hash():
    state = capture_rule_state(REPO_ROOT)
    assert state.code_state in (CODE_CLEAN, CODE_DIRTY, CODE_UNKNOWN)
    assert state.files[0].sha256 == compute_config_hash(REPO_ROOT / "config" / "thresholds.yaml")


# --- tables ----------------------------------------------------------------------------------------------------


def a_state(tmp_path):
    return capture_rule_state(make_repo(tmp_path))


def test_init_is_idempotent_and_creates_three_tables():
    connection = sqlite3.connect(":memory:")
    init_rule_state_tables(connection)
    init_rule_state_tables(connection)
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"run_rule_state", "run_rule_files", "rule_file_contents"} <= names


def test_write_stores_state_files_and_contents(tmp_path):
    state = a_state(tmp_path)
    connection = sqlite3.connect(":memory:")
    write_rule_state(connection, "stage2", "2026-10-08T10:00:00", state)
    assert connection.execute("SELECT step, run_timestamp, git_commit, code_state, dirty_paths FROM run_rule_state").fetchall() == [
        ("stage2", "2026-10-08T10:00:00", state.git_commit, CODE_CLEAN, None)
    ]
    assert connection.execute("SELECT COUNT(*) FROM run_rule_files").fetchone()[0] == len(RULE_FILES)
    assert connection.execute("SELECT COUNT(*) FROM rule_file_contents").fetchone()[0] == len(RULE_FILES)


def test_the_content_key_is_the_sha256_of_the_content(tmp_path):
    import hashlib

    state = a_state(tmp_path)
    connection = sqlite3.connect(":memory:")
    write_rule_state(connection, "stage1", "2026-10-08T10:00:00", state)
    for sha, content in connection.execute("SELECT sha256, content FROM rule_file_contents"):
        assert hashlib.sha256(content.encode("utf-8")).hexdigest() == sha


def test_the_same_files_in_two_runs_are_stored_once(tmp_path):
    state = a_state(tmp_path)
    connection = sqlite3.connect(":memory:")
    write_rule_state(connection, "stage1", "2026-10-08T10:00:00", state)
    write_rule_state(connection, "stage2", "2026-10-08T11:00:00", state)
    assert connection.execute("SELECT COUNT(*) FROM rule_file_contents").fetchone()[0] == len(RULE_FILES)
    assert connection.execute("SELECT COUNT(*) FROM run_rule_files").fetchone()[0] == 2 * len(RULE_FILES)


def test_a_repeated_step_and_run_timestamp_is_refused(tmp_path):
    state = a_state(tmp_path)
    connection = sqlite3.connect(":memory:")
    write_rule_state(connection, "trend", "2026-10-08T10:00:00", state)
    with pytest.raises(sqlite3.IntegrityError):
        write_rule_state(connection, "trend", "2026-10-08T10:00:00", state)


def test_an_unknown_step_is_refused(tmp_path):
    with pytest.raises(ValueError):
        write_rule_state(sqlite3.connect(":memory:"), "stage9", "2026-10-08T10:00:00", a_state(tmp_path))


def test_write_does_not_commit(tmp_path):
    db = tmp_path / "a.db"
    state = a_state(tmp_path)
    connection = sqlite3.connect(db)
    write_rule_state(connection, "stage1", "2026-10-08T10:00:00", state)
    connection.rollback()
    connection.close()
    check = sqlite3.connect(db)
    names = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = check.execute("SELECT COUNT(*) FROM run_rule_state").fetchone()[0] if "run_rule_state" in names else 0
    assert rows == 0
