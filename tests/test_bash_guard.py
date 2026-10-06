"""Tests for the PreToolUse hook src/hooks/bash_guard.py (Issue #60, Stufe 2).

Every known spelling of a refused command has a test, including rows 2 and 3
of the test protocol in docs/decisions.md ("Autonomer Betrieb"): `git -C .
push --dry-run origin master` and `bash -c "git push --dry-run origin
master"`. The allowed forms the agent really uses are tested as well, so the
hook cannot silently become a blanket refusal. Nothing here pushes anything:
the guard only parses strings; the real git calls run in a temporary repo.
"""

import ast
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from hooks import bash_guard
from hooks.bash_guard import (
    DEFAULT_CONFIG_PATH,
    EXIT_ALLOW,
    EXIT_BLOCK,
    Config,
    GuardConfigError,
    Guard,
    current_branch,
    evaluate,
    load_config,
    main,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "src" / "hooks" / "bash_guard.py"


@pytest.fixture(scope="module")
def config():
    return load_config()


def make_guard(config, branch="agent/thema", aliases=None):
    aliases = aliases or {}

    def alias_resolver(name, cdirs):
        return aliases.get(name)

    return Guard(config, cwd=".", branch_resolver=lambda cdirs: branch, alias_resolver=alias_resolver)


def refused(config, command, branch="agent/thema", aliases=None):
    return make_guard(config, branch, aliases).check(command)


# --------------------------------------------------------------------------
# git push: every spelling of "push to master/main" is refused
# --------------------------------------------------------------------------

PUSH_REFUSED = [
    # plain and test-protocol rows
    "git push origin master",
    "git push --dry-run origin master",  # protocol row 1
    "git -C . push --dry-run origin master",  # protocol row 2
    'bash -c "git push --dry-run origin master"',  # protocol row 3
    "git push --dry-run origin HEAD:master",  # protocol row 4
    "git push origin main",
    "git push origin MASTER",
    "git push origin Master",
    # refspec spellings
    "git push origin refs/heads/master",
    "git push origin HEAD:master",
    "git push origin HEAD:refs/heads/master",
    "git push origin HEAD:refs/heads/main",
    "git push origin agent/x:master",
    "git push origin agent/x:refs/heads/master",
    "git push origin heads/master",
    "git push origin refs/heads/master:refs/heads/master",
    "git push origin +HEAD:master",
    "git push origin +agent/x",
    "git push origin :agent/x",
    "git push origin :master",
    "git push origin agent/x master",
    "git push origin agent/x ma''ster",
    "git push origin agent/x 'mas'ter",
    'git push origin agent/x "m"aster',
    "git push origin agent/x ma\\ster",
    "git push origin agent/x HEAD:refs/heads/ma''ster",
    "git push origin 'refs/heads/*:refs/heads/*'",
    "git push origin 'agent/*:agent/*'",
    "git push origin agent/x $BRANCH",
    "git push origin agent/x ${BRANCH}",
    "git push origin agent/x $(git branch --show-current)",
    "git push origin agent/x `git branch --show-current`",
    "git push origin refs/tags/v1",
    "git push origin tags/v1",
    # options
    "git push --force origin agent/x",
    "git push --force-with-lease origin agent/x",
    "git push --force-with-lease=agent/x:abc origin agent/x",
    "git push --force-if-includes origin agent/x",
    "git push -f origin agent/x",
    "git push -uf origin agent/x",
    "git push origin agent/x -uf",
    "git push origin agent/x --force",
    "git push --forc origin agent/x",  # unique abbreviation of a long option
    "git push --delet origin agent/x",
    "git push --del origin agent/x",
    "git push --mirr origin",
    "git push --mirror origin",
    "git push --all origin",
    "git push --al origin",
    "git push --tags origin",
    "git push --tag origin",
    "git push --follow-tags origin agent/x",
    "git push --prune origin agent/x",
    "git push --delete origin agent/x",
    "git push -d origin agent/x",
    "git push --repo origin agent/x master",
    "git push --repo=origin master",
    "git push -- origin master",
    "git push -o ci.skip origin master",
    "git push --push-option=x origin master",
    # wrappers, global options, paths
    "git -C . push origin master",
    "git -C /tmp push origin master",
    "git -C . -C .. push origin master",
    "git -c user.name=x push origin master",
    "git -cuser.name=x push origin master",
    "git --git-dir=.git push origin master",
    "git --git-dir .git --work-tree . push origin master",
    "git --no-pager push origin master",
    "/usr/bin/git push origin master",
    "'git' push origin master",
    '"git" push origin master',
    "g''it push origin master",
    "\\git push origin master",
    "command git push origin master",
    "env git push origin master",
    "env -i GIT_DIR=x git push origin master",
    "FOO=1 git push origin master",
    "FOO=1 BAR=2 git push origin master",
    "timeout 30 git push origin master",
    "time git push origin master",
    "nohup git push origin master",
    "nice -n 5 git push origin master",
    "sudo git push origin master",
    "exec git push origin master",
    "setsid git push origin master",
    "stdbuf -oL git push origin master",
    # shells and eval
    "bash -c 'git push origin master'",
    "sh -c 'git push origin master'",
    "zsh -c 'git push origin master'",
    "dash -c 'git push origin master'",
    "bash -lc 'git push origin master'",
    "bash -xec 'git push origin master'",
    "bash -o pipefail -c 'git push origin master'",
    "bash --norc -c 'git push origin master'",
    "/bin/bash -c 'git push origin master'",
    "bash -c \"bash -c 'git push origin master'\"",
    "bash -c 'sh -c \"bash -c \\\"git push origin master\\\"\"'",
    "eval 'git push origin master'",
    "eval git push origin master",
    "eval \"git push origin ma''ster\"",
    "bash -c 'git -C . push origin master'",
    "bash -c 'cd /tmp && git push origin master'",
    # stdin and heredocs into shells
    "echo 'git push origin master' | bash",
    "echo 'git push origin master' | sh",
    "printf 'git push origin master' | bash -s",
    "bash <<< 'git push origin master'",
    "bash <<EOF\ngit push origin master\nEOF",
    "bash <<'EOF'\ngit push origin master\nEOF",
    "bash <<-EOF\n\tgit push origin master\n\tEOF",
    "cat <<EOF | bash\ngit push origin master\nEOF",
    "sh <<EOF\necho hi\ngit push origin master\nEOF",
    "echo '<<A'\ngit push origin master\nA",  # a quoted << is not a heredoc
    "bash\n",  # bare shell reads stdin
    "sh -s",
    "bash < script.sh",
    # xargs and friends
    "echo master | xargs git push origin",
    "printf master | xargs -I{} git push origin {}",
    "xargs git push origin master",
    "parallel git push origin ::: master",
    "find . -maxdepth 0 -exec git push origin master ';'",
    # compound commands, groups, newlines
    "true && git push origin master",
    "false || git push origin master",
    "true; git push origin master",
    "git status & git push origin master",
    "git status | git push origin master",
    "(git push origin master)",
    "(cd /tmp; git push origin master)",
    "{ git push origin master; }",
    "if git push origin master; then echo ok; fi",
    "for b in master; do git push origin master; done",
    "while true; do git push origin master; done",
    "git status\ngit push origin master",
    "git status\n\ngit push origin master\n",
    "git push \\\n  origin \\\n  master",
    "echo $(git push origin master)",
    "echo `git push origin master`",
    'echo "$(git push origin master)"',
    'echo "prefix $(git push origin master) suffix"',
    'echo "$(echo "$(git push origin master)")"',
    'printf "%s" "`git push origin master`"',
    'echo "`git push origin master`"',  # backticks execute inside double quotes
    'gh issue comment 60 --body "`git push origin master`"',
    "echo \\`git push origin master\\` | bash",
    'git commit -m "$(git push origin master)"',
    'VAR="$(git push origin master)"',
    'cat <(git push origin master)',
    "diff <(git status) <(git push origin master)",
    'echo "$(git -C . push origin master)"',
    "git push origin master # push",
    # variables / unknowable command words
    "$GIT push origin master",
    "${GIT} push origin master",
    "git $SUB origin master",
    "$(echo git) push origin master",
    "eval $CMD",
    # inline code in an interpreter
    "python3 -c \"import os; os.system('git push origin master')\"",
    "python -c \"import subprocess; subprocess.run(['git','push','origin','master'])\"",
    "python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['git', 'push', 'origin', 'master'])\nEOF",
    "python3 <<EOF\nimport os\nos.system('git push origin master')\nEOF",
    "node -e \"require('child_process').execSync('git push origin master')\"",
    "perl -e 'system(\"git push origin master\")'",
    "ruby -e 'system(\"git push origin master\")'",
    "python3 -c \"import os; os.system('gh pr merge 5')\"",
    "python3 -c \"import os; os.system('gh api repos/o/r/pulls/5/merge -X PUT')\"",
    "python3 -c \"import subprocess; subprocess.run('curl -X PUT https://api.github.com/x', shell=True)\"",
]


@pytest.mark.parametrize("command", PUSH_REFUSED)
def test_push_forms_are_refused(config, command):
    assert refused(config, command), command


def test_protocol_rows_2_and_3_are_refused_with_a_clear_reason(config):
    for command in ('git -C . push --dry-run origin master', 'bash -c "git push --dry-run origin master"'):
        reasons = refused(config, command)
        assert reasons and "protected branch 'master'" in reasons[0]


# implicit pushes depend on the current branch
IMPLICIT_PUSH = [
    "git push",
    "git push origin",
    "git push -u origin",
    "git push --dry-run origin",
    "git push origin HEAD",
    "git push origin @",
    "git push -u origin HEAD",
    "git -C . push",
    "bash -c 'git push'",
    "xargs git push",
]


@pytest.mark.parametrize("command", IMPLICIT_PUSH)
def test_implicit_push_on_master_is_refused(config, command):
    assert refused(config, command, branch="master"), command
    assert refused(config, command, branch="main"), command
    assert refused(config, command, branch="Master"), command


@pytest.mark.parametrize("command", [c for c in IMPLICIT_PUSH if "xargs" not in c])
def test_implicit_push_on_a_feature_branch_is_allowed(config, command):
    assert refused(config, command, branch="agent/thema") == [], command
    assert refused(config, command, branch=None) == [], command  # detached HEAD: nothing to push


def test_implicit_push_with_explicit_git_dir_is_refused_because_the_branch_is_unknown(config):
    assert refused(config, "git --git-dir=/x/.git push origin")


def test_branch_lookup_failure_fails_closed(config):
    def broken(cdirs):
        raise bash_guard.GuardError("git lookup failed")

    guard = Guard(config, cwd=".", branch_resolver=broken, alias_resolver=lambda n, c: None)
    assert guard.check("git push origin")
    assert guard.check("git push origin agent/x") == []  # explicit refspec needs no lookup


# git aliases
def test_alias_for_push_is_resolved(config):
    assert refused(config, "git p origin master", aliases={"p": "push"})
    assert refused(config, "git pf origin agent/x", aliases={"pf": "push --force"})
    assert refused(config, "git pm", aliases={"pm": "!git push origin master"})
    assert refused(config, "git pm", aliases={"pm": "push origin master"})
    assert refused(config, "git sneaky", aliases={"sneaky": "!bash -c 'git push origin master'"})


def test_harmless_alias_and_unknown_subcommand_are_allowed(config):
    assert refused(config, "git st", aliases={"st": "status"}) == []
    assert refused(config, "git nothing-here") == []
    assert refused(config, "git p origin agent/x", aliases={"p": "push"}) == []


def test_defining_an_alias_is_refused(config):
    assert refused(config, "git config alias.p push")
    assert refused(config, "git config --global alias.p push")
    assert refused(config, "git -c alias.p=push p origin master")
    assert refused(config, "git -c core.sshCommand=evil push origin agent/x")
    assert refused(config, "git -c core.hooksPath=/tmp/x commit -m x")


def test_unparseable_command_is_refused(config):
    assert refused(config, "git push origin 'master")
    assert refused(config, 'echo "unclosed')


def test_too_long_or_too_deep_commands_are_refused(config):
    assert refused(config, "echo " + "x" * (config.max_command_length + 1))
    nested = "git status"
    for _ in range(config.max_depth + 2):
        nested = "bash -c " + json.dumps(nested)
    assert refused(config, nested)


# --------------------------------------------------------------------------
# git push: what must stay allowed
# --------------------------------------------------------------------------

ALLOWED = [
    "ls -la",
    "git status",
    "git status -sb",
    "git log --oneline -5",
    "git diff --stat HEAD~1",
    "git fetch origin",
    "git fetch origin master",
    "git pull --ff-only origin master",
    "git checkout -b agent/hook-stufe2",
    "git checkout master",
    "git branch --show-current",
    "git push origin agent/hook-stufe2",
    "git push -u origin agent/hook-stufe2",
    "git push origin agent/hook-stufe2 --dry-run",
    "git push --dry-run origin agent/hook-stufe2",
    "git push origin agent/x:agent/x",
    "git push origin refs/heads/agent/x:refs/heads/agent/x",
    "git push origin HEAD:agent/x",
    "git push origin agent/master",
    "git push origin agent/main-fix",
    "git push origin master:agent/x",  # sends master's content to a feature branch
    "git push -o ci.skip origin agent/x",
    "git push --set-upstream origin agent/x",
    "git push --verbose origin agent/x",
    "git push --no-verify origin agent/x",
    "git push --porcelain origin agent/x",
    "git add src/hooks/bash_guard.py config/bash_guard.yaml",
    "git add -- src/a.py",
    "git add .claude/settings.json CLAUDE.md docs/arbeitsweise.md",
    "git add -N src/new.py",
    "git add docs/decisions.md",
    "git commit -m 'Autonomer Betrieb: Hook'",
    'git commit -m "message that mentions git push origin master and git add -A"',
    "git commit -m '-a is only text here'",
    "git commit -m msg -s",
    "git commit -sm msg",
    "git commit --amend --no-edit",
    "git commit -F message.txt",
    "gh pr create --base master --title 'x' --body 'y'",
    "gh pr create --base master --head agent/x --title x --body-file body.md",
    "gh pr view 55",
    "gh pr view 55 --json state,headRefOid",
    "gh pr list",
    "gh pr diff 59",
    "gh issue view 60",
    "gh issue list",
    "gh issue comment 60 --body 'merge nothing, master untouched'",
    "gh api repos/kdaubner5/equity-research/pulls/55",
    "gh api repos/kdaubner5/equity-research/branches/master",
    "gh api -X GET repos/kdaubner5/equity-research/issues -f per_page=100",
    "gh api --method GET repos/o/r/pulls -F state=open",
    "gh -R kdaubner5/equity-research pr view 55",
    "~/venvs/equity/bin/python -m pytest -q",
    "~/venvs/equity/bin/pytest tests/test_bash_guard.py -q",
    "$HOME/venvs/equity/bin/python -m pytest -q",
    "python3 -c 'print(1)'",
    "python3 - <<'EOF'\nprint('git push origin master is only text')\nEOF",
    "python3 - <<'EOF'\ntext = open('docs/decisions.md').read()\nopen('docs/decisions.md', 'w').write(text.replace('git push origin master', 'x'))\nEOF",
    "cat > notes.md <<'EOF'\ngit push origin master\ngit add -A\nEOF",
    "cat <<EOF\ngit push origin master\nEOF",
    "echo 'git push origin master'",
    'echo "git push --force origin master"',
    "printf 'gh pr merge 5\\n'",
    "grep -n 'git push' CLAUDE.md",
    "grep -rn 'gh pr merge' docs",
    "which git",
    "bash script.sh",
    "bash --version",
    "bash -c 'echo hello'",
    "sh -c 'ls; pwd'",
    "curl https://api.github.com/repos/kdaubner5/equity-research",
    "curl -X PUT https://example.com/resource",
    "curl -s https://api.github.com/repos/o/r/pulls/5 -o out.json",
    "wget -q https://api.github.com/repos/o/r",
    "FOO=bar make",
    "rm output.txt",
    "mkdir -p tmp && cd tmp && ls",
    "git push origin agent/x && git status",
    "git push origin agent/x\ngit status",
    "echo done # git push origin master",
    # single quotes are literal in bash: backticks in markdown text are not commands
    "gh pr create --base master --title x --body 'Hook `bash_guard` lehnt `git push origin master` und `git add -A` ab'",
    "gh issue comment 60 --body 'siehe `gh pr merge 5` und `git commit -a`'",
    "git commit -m 'Doku: `git push --force` ist verboten'",
    "gh pr create --body 'Zeile eins\n`git push origin master`\nZeile drei'",
    "echo '`git push origin master`'",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_normal_commands_are_allowed(config, command):
    assert refused(config, command) == [], command


# --------------------------------------------------------------------------
# git add / commit
# --------------------------------------------------------------------------

ADD_REFUSED = [
    "git add -A",  # named in the order
    "git add --all",
    "git add -A src",
    "git add src -A",
    "git add .",
    "git add ./",
    "git add ..",
    "git add .//",
    "git add *",
    "git add src/*",
    "git add '*.py'",
    "git add src/?.py",
    "git add 'src/[ab].py'",
    "git add -u",
    "git add --update",
    "git add -f src/a.py",
    "git add --force src/a.py",
    "git add -fA .",
    "git add -fn x",
    "git add -- .",
    "git add :/",
    "git add ':(top)'",
    "git add ':(glob)**/*.py'",
    "git add --pathspec-from-file=list.txt",
    "git add .env",
    "git add .env.local",
    "git add ./.env",
    "git add config/../.env",
    "git add data",
    "git add data/",
    "git add data/run_archive.db",
    "git add ./data/cache",
    "git add $(git ls-files)",
    "git add `git ls-files -m`",
    "git add $FILES",
    "git -C . add -A",
    "bash -c 'git add -A'",
    "env git add -A",
    "git add --al",  # abbreviation of --all
    "git add --upd",
    "git commit -a",
    "git commit -am 'x'",
    "git commit -a -m 'x'",
    "git commit -sa -m x",
    "git commit --all -m x",
    "git commit -m x -a",
]


@pytest.mark.parametrize("command", ADD_REFUSED)
def test_stage_everything_forms_are_refused(config, command):
    assert refused(config, command), command


# --------------------------------------------------------------------------
# gh pr merge / gh api
# --------------------------------------------------------------------------

GH_REFUSED = [
    "gh pr merge 55",
    "gh pr merge 55 --merge",
    "gh pr merge 55 --squash --auto",
    "gh pr merge",
    "gh pr merge --admin 55",
    "gh -R o/r pr merge 55",
    "gh --repo o/r pr merge 55",
    "gh --repo=o/r pr merge 55",
    "gh pr  merge 55",
    "GH_REPO=o/r gh pr merge 55",
    "/usr/local/bin/gh pr merge 55",
    "gh alias set m 'pr merge'",
    "gh alias import aliases.yml",
    "echo 55 | xargs gh pr merge",
    "bash -c 'gh pr merge 55'",
    "env gh pr merge 55",
    # api: explicit write methods on merge-like endpoints
    "gh api -X PUT repos/o/r/pulls/55/merge",
    "gh api repos/o/r/pulls/55/merge -X PUT",
    "gh api --method PUT repos/o/r/pulls/55/merge",
    "gh api --method=PUT repos/o/r/pulls/55/merge",
    "gh api -XPUT repos/o/r/pulls/55/merge",
    "gh api -X POST repos/o/r/merges -f base=master -f head=agent/x",
    "gh api -X put repos/o/r/pulls/55/merge",
    "gh api repos/o/r/pulls/55/merge -f merge_method=merge",
    "gh api repos/o/r/pulls/55/merge -F merge_method=merge",
    "gh api repos/o/r/pulls/55/merge --field merge_method=merge",
    "gh api repos/o/r/pulls/55/merge --raw-field merge_method=merge",
    "gh api repos/o/r/pulls/55/merge --input body.json",
    "gh api repos/o/r/pulls/55/merge -fmerge_method=merge",
    "gh api -X DELETE repos/o/r/git/refs/heads/master",
    "gh api -X DELETE repos/o/r/git/refs/heads/main",
    "gh api -X PATCH repos/o/r/git/refs/heads/master -f sha=abc -F force=true",
    "gh api -X POST repos/o/r/git/refs -f ref=refs/heads/master -f sha=abc",
    "gh api -X PUT repos/o/r/contents/README.md -f branch=master -f message=x",
    "gh api repos/o/r/contents/README.md -f branch=main -f message=x",
    "gh api graphql -f query='mutation { mergePullRequest(input:{pullRequestId:\"X\"}) { clientMutationId } }'",
    "gh api /graphql -fquery='mutation{mergePullRequest(input:{pullRequestId:\"X\"}){clientMutationId}}'",
    "gh api graphql -F query=@merge.graphql",
    "gh api graphql -f query='mutation { enablePullRequestAutoMerge(input:{pullRequestId:\"X\"}) { clientMutationId } }'",
    "gh api graphql -f query='mutation { updateRef(input:{refId:\"X\", oid:\"abc\"}) { clientMutationId } }'",
    "gh api repos/o/r/contents/x --input body.json -X PUT",
    "gh api repos/o/r/issues -F body=@secrets.txt",
    "gh api -X POST repos/o/r/issues -f title=x -f body=y -f branch=Master",
    "bash -c 'gh api -X PUT repos/o/r/pulls/55/merge'",
    # raw HTTP to the GitHub API
    "curl -X PUT https://api.github.com/repos/o/r/pulls/55/merge",
    "curl --request PUT https://api.github.com/repos/o/r/pulls/55/merge",
    "curl -XPUT https://api.github.com/repos/o/r/pulls/55/merge",
    "curl -d '{}' https://api.github.com/repos/o/r/pulls/55/merge",
    "curl --data '{}' https://api.github.com/repos/o/r/issues",
    "curl --json '{}' https://api.github.com/repos/o/r/issues",
    "curl -H 'Authorization: token x' -X DELETE https://api.github.com/repos/o/r/git/refs/heads/master",
    "wget --method=PUT https://api.github.com/repos/o/r/pulls/55/merge",
    "wget --post-data='{}' https://api.github.com/repos/o/r/issues",
    "curl -X POST https://api.github.com/repos/o/r/issues -d '{}'",
]


@pytest.mark.parametrize("command", GH_REFUSED)
def test_gh_and_api_merge_forms_are_refused(config, command):
    assert refused(config, command), command


# --------------------------------------------------------------------------
# hook entry point: exit codes, stderr, fail closed
# --------------------------------------------------------------------------

def payload(command, tool="Bash", **extra):
    data = {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(REPO_ROOT)}
    data.update(extra)
    return data


def run_main(data, config_path=DEFAULT_CONFIG_PATH):
    stdin = io.StringIO(data if isinstance(data, str) else json.dumps(data))
    stderr = io.StringIO()
    code = main(stdin=stdin, stderr=stderr, config_path=config_path)
    return code, stderr.getvalue()


def test_main_blocks_with_exit_code_2_and_explains(config):
    code, err = run_main(payload("git -C . push --dry-run origin master"))
    assert code == EXIT_BLOCK == 2
    assert "REFUSED" in err and "master" in err


def test_main_allows_silently_with_exit_code_0():
    code, err = run_main(payload("git push origin agent/hook-stufe2"))
    assert code == EXIT_ALLOW == 0
    assert err == ""


def test_main_ignores_other_tools():
    assert run_main(payload("git push origin master", tool="Read"))[0] == EXIT_ALLOW


@pytest.mark.parametrize("permission_mode", ["default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan"])
def test_the_decision_does_not_depend_on_the_permission_mode(permission_mode):
    assert run_main(payload("git push origin master", permission_mode=permission_mode))[0] == EXIT_BLOCK
    assert run_main(payload("git status", permission_mode=permission_mode))[0] == EXIT_ALLOW


@pytest.mark.parametrize(
    "bad_input",
    [
        "",
        "not json",
        "[]",
        "null",
        '{"tool_name": "Bash"}',
        '{"tool_name": "Bash", "tool_input": {}}',
        '{"tool_name": "Bash", "tool_input": {"command": 5}}',
        '{"tool_name": "Bash", "tool_input": "git push origin master"}',
    ],
)
def test_main_fails_closed_on_malformed_input(bad_input):
    code, err = run_main(bad_input)
    assert code == EXIT_BLOCK
    assert err


def test_main_fails_closed_when_the_config_is_missing(tmp_path):
    code, err = run_main(payload("git status"), config_path=tmp_path / "missing.yaml")
    assert code == EXIT_BLOCK and "fail closed" in err


def test_main_fails_closed_when_the_config_is_malformed(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("protected_branches: [master]\n")
    code, _ = run_main(payload("git status"), config_path=bad)
    assert code == EXIT_BLOCK
    bad.write_text("::: not yaml :::\n  - [")
    assert run_main(payload("git status"), config_path=bad)[0] == EXIT_BLOCK


def test_script_end_to_end_exit_codes():
    """Run the script as Claude Code does (stdin JSON, real exit code)."""
    def run(command):
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(payload(command)),
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )

    blocked = run("git -C . push --dry-run origin master")
    assert blocked.returncode == 2 and "REFUSED" in blocked.stderr
    blocked = run('bash -c "git push --dry-run origin master"')
    assert blocked.returncode == 2
    allowed = run("git push origin agent/hook-stufe2 --dry-run")
    assert allowed.returncode == 0 and allowed.stderr == ""


def test_script_with_empty_stdin_exits_2():
    result = subprocess.run([sys.executable, str(SCRIPT)], input="", capture_output=True, text=True)
    assert result.returncode == 2


def test_evaluate_reports_reasons(config):
    allowed, reasons = evaluate(payload("git add -A"), config)
    assert not allowed and reasons
    assert evaluate(payload("git status"), config) == (True, [])


# --------------------------------------------------------------------------
# real git: current branch resolution (temporary repository)
# --------------------------------------------------------------------------

def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "master")
    return tmp_path


def test_current_branch_reads_the_real_branch(repo):
    assert current_branch([], repo) == "master"
    git(repo, "checkout", "-q", "-b", "agent/x")
    assert current_branch([], repo) == "agent/x"
    assert current_branch(["."], repo) == "agent/x"


def test_current_branch_outside_a_repository_is_none(tmp_path):
    assert current_branch([], tmp_path) is None


def test_default_resolver_refuses_implicit_push_on_master_and_allows_it_elsewhere(config, repo):
    on_master = payload("git push", cwd=str(repo))
    assert evaluate(on_master, config)[0] is False
    assert evaluate(payload("git push origin HEAD", cwd=str(repo)), config)[0] is False
    assert evaluate(payload("git -C . push", cwd=str(repo)), config)[0] is False
    git(repo, "checkout", "-q", "-b", "agent/x")
    assert evaluate(payload("git push", cwd=str(repo)), config)[0] is True
    assert evaluate(payload("git push origin master", cwd=str(repo)), config)[0] is False


def test_default_alias_resolver_reads_the_real_git_config(config, repo):
    git(repo, "config", "alias.pm", "push origin master")
    git(repo, "config", "alias.st", "status")
    assert evaluate(payload("git pm", cwd=str(repo)), config)[0] is False
    assert evaluate(payload("git st", cwd=str(repo)), config)[0] is True


# --------------------------------------------------------------------------
# configuration: patterns live in YAML, not in code
# --------------------------------------------------------------------------

def test_real_config_loads_and_protects_master_and_main(config):
    assert {"master", "main"} <= set(config.protected)
    assert isinstance(config, Config)


def _minimal_config():
    return yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "key",
    ["protected_branches", "max_command_length", "max_depth", "non_executing_commands", "shell_interpreters", "git", "gh", "http", "inline_deny_patterns"],
)
def test_every_top_level_key_is_required(key):
    data = _minimal_config()
    del data[key]
    with pytest.raises(GuardConfigError):
        Config(data)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["git"]["push"].pop("refused_long_options"),
        lambda d: d["git"]["add"].pop("refused_pathspec_patterns"),
        lambda d: d["gh"].pop("api"),
        lambda d: d["gh"].update(refused_subcommands=[["pr"]]),
        lambda d: d.update(protected_branches=[]),
        lambda d: d.update(max_depth=0),
        lambda d: d.update(max_depth="8"),
        lambda d: d.update(inline_deny_patterns=["(unclosed"]),
        lambda d: d.update(shell_interpreters=[""]),
    ],
)
def test_malformed_config_is_rejected(mutate):
    data = _minimal_config()
    mutate(data)
    with pytest.raises(GuardConfigError):
        Config(data)


def test_protected_branches_are_configurable():
    data = _minimal_config()
    data["protected_branches"] = ["release"]
    cfg = Config(data)
    assert refused(cfg, "git push origin release")
    assert refused(cfg, "git push origin master") == []


def test_no_branch_name_is_hardcoded_in_the_code():
    """Thresholds and patterns belong in config/ (CLAUDE.md): the protected
    branch names must appear in the code only inside docstrings/comments."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                docstrings.add(id(body[0].value))
    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
        and re.search(r"\b(master|main)\b", node.value)
    ]
    assert offenders == []


def test_hook_is_registered_in_settings_json():
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    entries = settings["hooks"]["PreToolUse"]
    bash_entries = [e for e in entries if e["matcher"] == "Bash"]
    assert bash_entries, "no PreToolUse hook for Bash"
    command = bash_entries[0]["hooks"][0]["command"]
    assert "src/hooks/bash_guard.py" in command
    assert "|| exit 2" in command  # a crash of the interpreter must block, not pass
