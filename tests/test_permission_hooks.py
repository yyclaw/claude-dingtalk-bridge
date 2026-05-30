from pathlib import Path

import bashlex
import pytest

from claude_dingtalk_bridge.config import PermissionRules
from claude_dingtalk_bridge.permission_hooks import (
    Decision,
    _c_wrapper_inner,
    _cd_targets,
    _deny_atom,
    _eval_inner,
    _edit_target,
    _protected_write_reason,
    _redirect_write_target,
    _rule_matches,
    _strip_transparent,
    _walk,
    _writer_targets,
    decide_bash,
    decide_edit,
    make_bash_permission_hook,
    make_edit_path_hook,
    tripwire_match,
)


def _rules(deny=None) -> PermissionRules:
    return PermissionRules(deny=list(deny or []))


def _input(tool_name: str, command: str = "") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "s",
        "cwd": "/tmp/p",
        "tool_name": tool_name,
        "tool_input": {"command": command} if command else {},
        "tool_use_id": "u",
    }


def _verdict(out: dict) -> str | None:
    spec = out.get("hookSpecificOutput") if out else None
    return spec.get("permissionDecision") if spec else None


# ----------------------------------------------------------------------------
# Built-in tripwires — fire regardless of config.
# ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "rm -f /tmp/foo",
        "rm -rf /tmp/foo",
        "rm -fr /tmp/foo",
        "rm -Rf /tmp/foo",
        "rm -rfv /tmp/foo",
        "rm -vrf /tmp/foo",
        "rm -r -f /tmp/foo",          # force split across short-flag groups
        "rm -f -r /tmp/foo",          # force-first, split
        "rm -i -r -f /tmp/foo",       # force in a trailing group
        "rm --recursive -f /tmp/foo",  # long recursive + short force
        "rm --force /tmp/foo",
        "rm --recursive --force /tmp/foo",
        "rm --force --recursive /tmp/foo",
        "dd if=/dev/zero of=/dev/sda bs=1M",
        "mkfs.ext4 /dev/sda1",
        "mkfs /dev/sda1",
        "cat foo > /dev/sda",
        "echo x > /dev/nvme0n1",
        ":(){ :|:& };:",
    ],
)
def test_tripwire_matches_dangerous(command):
    assert tripwire_match(command) != ""


@pytest.mark.parametrize(
    "command",
    [
        "rm foo",
        "rm -r foo",        # recursive only — by design, not built-in
        "rm -i -r foo",     # interactive + recursive, still no force
        "rm -i foo",
        "ls -lh",
        "echo dd",
        "cat /dev/null",
        "grep -r needle src/",
    ],
)
def test_tripwire_no_match_safe(command):
    assert tripwire_match(command) == ""


@pytest.mark.parametrize(
    "command",
    [
        '"rm" -rf /tmp/x',          # quoted command name
        "'rm' -rf /tmp/x",
        'r"m" -rf /tmp/x',          # quote splitting a name
        "\\rm -rf /tmp/x",          # backslash-escaped name
        "/bin/rm -rf /tmp/x",       # absolute path
        "/usr/bin/rm --force /tmp/x",
        'dd if=/dev/zero of="/dev/sda"',   # quoted device target
        "mk''fs.ext4 /dev/sda1",    # split-word mkfs
        'cat foo > "/dev/sda"',     # quoted block-device redirect
    ],
)
def test_tripwire_matches_after_normalization(command):
    # The raw-string pass misses these (quoting / path / split words); the
    # parsed pass rebuilds the atom from bashlex's unquoted words and matches.
    assert tripwire_match(command) != ""


@pytest.mark.parametrize(
    "command",
    [
        "'rm' -r /tmp/x",           # recursive-only stays uncaught after unquoting
        "/bin/ls -la",
        "confirm --force thing",    # 'rm'/'dd' substrings inside a word, not atoms
    ],
)
def test_tripwire_parsed_pass_no_false_positive(command):
    assert tripwire_match(command) == ""


# ----------------------------------------------------------------------------
# decide_bash — "ask" on tripwire / deny match, "pass" otherwise.
# ----------------------------------------------------------------------------

def test_decide_pass_when_no_rules_and_no_tripwire():
    assert decide_bash("git status", None).verdict == "pass"
    assert decide_bash("git status", _rules()).verdict == "pass"


def test_decide_tripwire_overrides_no_rules():
    # No deny rules configured, but the literal pattern still triggers.
    d = decide_bash("rm -rf /tmp/x", _rules())
    assert d.verdict == "ask"
    assert "rm -f" in d.reason


def test_decide_deny_rule_match_atomic():
    # bashlex decomposes the chain; the rm atom matches the deny rule.
    d = decide_bash("cd /tmp && touch foo", _rules(deny=["Bash(touch:*)"]))
    assert d.verdict == "ask"
    assert "Bash(touch:*)" in d.reason


def test_decide_deny_rule_strips_transparent_prefix():
    # `exec touch` is just `touch` — the deny rule should still catch it.
    d = decide_bash("exec touch /tmp/foo", _rules(deny=["Bash(touch:*)"]))
    assert d.verdict == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "exec -a foo go run ./x",   # exec's -a takes a name argument
        "command -p go run ./x",    # command's -p flag
        "env -i go run ./x",        # env clears the environment
        "env -u PATH go run ./x",   # env's -u takes a var-name argument
    ],
)
def test_decide_wrapper_option_flags_escalate(command):
    # A transparent wrapper followed by its own option flags leaves an argv we
    # can't safely strip; rather than let the denied `go run` slip through as a
    # mangled prefix, escalate to the phone.
    d = decide_bash(command, _rules(deny=["Bash(go run:*)"]))
    assert d.verdict == "ask"
    assert "opaque wrapper option" in d.reason


def test_decide_wrapper_no_flags_still_strips():
    # Control: the same wrapper with no option flags strips cleanly and the
    # deny rule matches the real command.
    d = decide_bash("exec go run ./x", _rules(deny=["Bash(go run:*)"]))
    assert d.verdict == "ask"
    assert "go run" in d.reason


def test_decide_deny_rule_unwraps_bash_c():
    # bash -c '<cmd>' is recursively parsed.
    d = decide_bash(
        "bash -c 'touch /tmp/x'", _rules(deny=["Bash(touch:*)"])
    )
    assert d.verdict == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "eval 'touch /tmp/x'",     # quoted string form
        "eval touch /tmp/x",       # unquoted, args concatenated
        "eval \"touch $F\"",       # interpolated target, command still literal
    ],
)
def test_decide_eval_recurses_like_bash_c(command):
    # `eval STRING` runs STRING as a command, so it is recursed into the deny
    # matcher exactly like `sh -c` — closing the asymmetry where `bash -c` was
    # caught but `eval` slipped through.
    d = decide_bash(command, _rules(deny=["Bash(touch:*)"]))
    assert d.verdict == "ask"
    assert "Bash(touch:*)" in d.reason


def test_decide_eval_variable_command_name():
    # An eval'd command whose name is a variable is escalated even with no deny
    # rules, mirroring `bash -c '$C evil'`.
    d = decide_bash("eval \"$C evil\"", _rules())
    assert d.verdict == "ask"
    assert "variable" in d.reason


def test_decide_eval_benign_does_not_escalate():
    # Control: an eval'd command that matches no deny rule falls through.
    assert decide_bash("eval 'echo hi'", _rules(deny=["Bash(touch:*)"])).verdict == "pass"


@pytest.mark.parametrize(
    "command",
    [
        "/bin/rm -r /tmp/x",        # absolute path
        "/usr/bin/rm -r /tmp/x",
        "./rm -r /tmp/x",           # relative path
        "cd /tmp && /bin/rm -r y",  # path form buried in a chain
    ],
)
def test_decide_deny_rule_matches_path_prefixed_command(command):
    # A deny on the bare name (`Bash(rm:*)`) must also catch the program invoked
    # by path; the deny matcher tries the basename form when a slash is present.
    d = decide_bash(command, _rules(deny=["Bash(rm:*)"]))
    assert d.verdict == "ask"
    assert "Bash(rm:*)" in d.reason


def test_decide_deny_rule_explicit_path_form_still_matches():
    # Regression: an explicit path-form deny still matches the path invocation
    # (the raw candidate is tried alongside the basename one).
    d = decide_bash("/bin/rm -r /tmp/x", _rules(deny=["Bash(/bin/rm:*)"]))
    assert d.verdict == "ask"
    assert "Bash(/bin/rm:*)" in d.reason


def test_decide_deny_rule_basename_no_false_match():
    # A slash-free command is unaffected: `Bash(rm:*)` does not match `confirm`.
    assert decide_bash("/bin/confirm x", _rules(deny=["Bash(rm:*)"])).verdict == "pass"


def test_decide_pass_when_deny_rules_dont_match():
    d = decide_bash("git status -s", _rules(deny=["Bash(rm:*)"]))
    assert d.verdict == "pass"


def test_decide_escape_construct_asks_when_deny_configured():
    # Command substitution hides what's inside — paranoid: ask.
    d = decide_bash("echo $(rm foo)", _rules(deny=["Bash(rm:*)"]))
    assert d.verdict == "ask"
    assert "unsupported construct" in d.reason


def test_decide_escape_construct_pass_when_no_deny():
    # No deny rules → no bashlex pass → no escape detection. Settings layer
    # gets to decide.
    d = decide_bash("echo $(date)", _rules())
    assert d.verdict == "pass"


@pytest.mark.parametrize(
    "command",
    [
        ".venv/bin/pytest tests/test_x.py -q 2>&1 | tail -15",  # fd dup
        "make test >&2",                                        # fd dup to stderr
        "cmd > /dev/null",                                      # bit bucket
        "cmd 2>/dev/null",                                      # bit bucket (stderr)
        "cmd > /dev/null 2>&1",                                 # both, common idiom
        "cmd < /dev/null",                                      # bit bucket (stdin)
    ],
)
def test_decide_benign_redirect_does_not_degrade(command):
    # fd duplications and /dev/null redirects no longer force a phone ask even
    # when deny rules are configured — they hide no sub-command and write
    # nothing real.
    assert decide_bash(command, _rules(deny=["Bash(rm:*)"])).verdict == "pass"


@pytest.mark.parametrize(
    "command",
    [
        "echo x > out.txt",        # real file write
        "echo x >> log.txt",       # real file append
        "echo x > ~/.bashrc",      # sensitive real-file target
        "cmd >& realfile",         # >& to a word = redirect both streams to file
        "cat < input.txt",         # read a real file
    ],
)
def test_decide_file_redirect_still_degrades(command):
    # A redirect that touches a real path stays opaque: it could overwrite a
    # sensitive file (e.g. ~/.ssh/authorized_keys), bypassing edit gating.
    d = decide_bash(command, _rules(deny=["Bash(rm:*)"]))
    assert d.verdict == "ask"
    assert "unsupported construct" in d.reason


def test_decide_heredoc_still_degrades():
    # A heredoc can feed executable text to an interpreter — keep asking.
    d = decide_bash("bash <<EOF\nrm -rf /\nEOF", _rules(deny=["Bash(rm:*)"]))
    assert d.verdict == "ask"


def test_decide_benign_redirect_still_checks_deny_atom():
    # Skipping the redirect must not skip the command itself.
    d = decide_bash("rm foo > /dev/null", _rules(deny=["Bash(rm:*)"]))
    assert d.verdict == "ask"
    assert "Bash(rm:*)" in d.reason


def test_decide_unparseable_asks_when_deny_configured():
    d = decide_bash("rm ' unbalanced", _rules(deny=["Bash(rm:*)"]))
    assert d.verdict == "ask"
    assert "unparseable" in d.reason


def test_decide_unparseable_pass_when_no_deny():
    # Without deny rules we don't even parse; let SDK decide.
    d = decide_bash("rm ' unbalanced", _rules())
    assert d.verdict == "pass"


# ----------------------------------------------------------------------------
# make_bash_permission_hook — hook output mapping.
# ----------------------------------------------------------------------------

async def test_hook_non_bash_returns_empty():
    hook = make_bash_permission_hook(_rules(deny=["Bash"]))
    assert await hook(_input("Write", ""), None, {}) == {}


async def test_hook_empty_command_returns_empty():
    hook = make_bash_permission_hook(_rules(deny=["Bash"]))
    assert await hook(_input("Bash", ""), None, {}) == {}


async def test_hook_safe_command_no_rules_returns_empty():
    hook = make_bash_permission_hook(_rules())
    assert await hook(_input("Bash", "git status"), None, {}) == {}


async def test_hook_tripwire_returns_ask():
    hook = make_bash_permission_hook(_rules())
    out = await hook(_input("Bash", "rm -rf ~/foo"), None, {})
    assert _verdict(out) == "ask"
    assert "rm -f" in out["hookSpecificOutput"]["permissionDecisionReason"]


async def test_hook_deny_rule_returns_ask():
    hook = make_bash_permission_hook(_rules(deny=["Bash(touch:*)"]))
    out = await hook(_input("Bash", "cd /tmp && touch x"), None, {})
    assert _verdict(out) == "ask"


async def test_hook_none_rules_still_runs_tripwire():
    # Even with no rules at all the built-in tripwire fires.
    hook = make_bash_permission_hook(None)
    out = await hook(_input("Bash", "rm -rf /tmp/x"), None, {})
    assert _verdict(out) == "ask"


async def test_hook_none_rules_pass_for_safe():
    hook = make_bash_permission_hook(None)
    assert await hook(_input("Bash", "git status"), None, {}) == {}


async def test_hook_node_eval_not_caught():
    # Documented bypass: interpreter wrappers hide the dangerous call inside
    # an opaque argument string; the tripwire (regex on the surface) and the
    # bashlex atom matcher both see only the outer `node`.
    hook = make_bash_permission_hook(_rules(deny=["Bash(rm:*)"]))
    out = await hook(
        _input(
            "Bash",
            "node -e 'require(\"child_process\").execSync(\"rm -rf /tmp/x\")'",
        ),
        None,
        {},
    )
    # The inner `rm` IS visible to the raw-string tripwire — caught.
    assert _verdict(out) == "ask"


async def test_hook_node_eval_no_literal_rm_passes():
    # Bypass that hides even the literal `rm` substring slips through.
    hook = make_bash_permission_hook(_rules(deny=["Bash(rm:*)"]))
    out = await hook(
        _input(
            "Bash",
            "node -e 'require(\"fs\").rmSync(\"/tmp/x\")'",
        ),
        None,
        {},
    )
    assert out == {}


# ----------------------------------------------------------------------------
# _walk — full traversal recurses into nested .command / .list nodes.
# These branches are unreachable through _deny_atom (it returns at the first
# escape-kind node), so exercise the walker directly.
# ----------------------------------------------------------------------------

def _node_kinds(command: str) -> set[str]:
    kinds: set[str] = set()
    for tree in bashlex.parse(command):
        for node in _walk(tree):
            kind = getattr(node, "kind", None)
            if kind is not None:
                kinds.add(kind)
    return kinds


def test_walk_recurses_into_command_substitution():
    # The substitution's inner `.command` (lines 130-132) is only seen when
    # the walk is fully consumed.
    kinds = _node_kinds("echo $(touch x)")
    assert "commandsubstitution" in kinds
    # The inner `touch` command node is reached via .command recursion.
    assert "command" in kinds


def test_walk_recurses_into_compound_list():
    # A compound node carries its body in `.list` (lines 133-135).
    kinds = _node_kinds("( touch x )")
    assert "compound" in kinds
    assert "command" in kinds


def test_walk_skips_list_child_without_kind():
    # Defensive guard: a `.list` entry that isn't an AST node (no `.kind`) is
    # skipped rather than recursed into. Real bashlex never emits this, so
    # exercise it with a stub node.
    class _Stub:
        pass

    node = _Stub()
    node.kind = "compound"
    node.list = [object()]  # a non-node child — hasattr(child, "kind") is False
    assert list(_walk(node)) == [node]


# ----------------------------------------------------------------------------
# _strip_transparent — leading assignments, env, and arg-bearing wrappers.
# ----------------------------------------------------------------------------

def test_strip_transparent_drops_leading_assignment():
    assert _strip_transparent(["FOO=bar", "touch", "x"]) == ["touch", "x"]


def test_strip_transparent_assignment_only_becomes_empty():
    # Stripping the sole assignment empties argv and breaks the loop.
    assert _strip_transparent(["FOO=bar"]) == []


def test_strip_transparent_env_with_assignments():
    assert _strip_transparent(["env", "FOO=bar", "BAZ=1", "touch", "x"]) == [
        "touch",
        "x",
    ]


def test_strip_transparent_arg_wrapper_consumes_flags_and_numbers():
    assert _strip_transparent(["timeout", "5", "touch", "x"]) == ["touch", "x"]
    assert _strip_transparent(["nice", "-n", "10", "rm", "y"]) == ["rm", "y"]


# ----------------------------------------------------------------------------
# _c_wrapper_inner — locate the -c payload, or None.
# ----------------------------------------------------------------------------

def test_c_wrapper_inner_skips_non_c_flag():
    assert _c_wrapper_inner(["bash", "-x", "-c", "touch x"]) == "touch x"


def test_c_wrapper_inner_none_when_no_c_flag():
    assert _c_wrapper_inner(["sh", "script.sh"]) is None


def test_c_wrapper_inner_c_not_at_end_of_group():
    # `bash -cl` / `bash -cx` still read the command from the next argument —
    # the `c` need not be the last letter of the short-flag group.
    assert _c_wrapper_inner(["bash", "-cl", "touch x"]) == "touch x"
    assert _c_wrapper_inner(["bash", "-cx", "touch x"]) == "touch x"


def test_decide_cl_flag_order_unwraps_inner():
    # Regression: `-cl` ordering must not slip the inner command past deny.
    d = decide_bash("bash -cl 'touch /tmp/x'", _rules(deny=["Bash(touch:*)"]))
    assert d.verdict == "ask"


def test_eval_inner_joins_args():
    assert _eval_inner(["eval", "rm -r x"]) == "rm -r x"
    assert _eval_inner(["eval", "rm", "-r", "x"]) == "rm -r x"
    assert _eval_inner(["/bin/eval", "rm x"]) == "rm x"  # path form normalized
    assert _eval_inner(["eval"]) == ""                   # no args -> empty inner


def test_eval_inner_none_for_non_eval():
    assert _eval_inner(["bash", "-c", "x"]) is None
    assert _eval_inner([]) is None


# ----------------------------------------------------------------------------
# sudo / xargs are transparent wrappers for deny matching.
# ----------------------------------------------------------------------------

def test_strip_transparent_strips_sudo():
    assert _strip_transparent(["sudo", "touch", "x"]) == ["touch", "x"]


def test_strip_transparent_strips_xargs():
    assert _strip_transparent(["xargs", "touch"]) == ["touch"]


def test_decide_sudo_deny_match():
    d = decide_bash("sudo touch /tmp/x", _rules(deny=["Bash(touch:*)"]))
    assert d.verdict == "ask"
    assert "Bash(touch:*)" in d.reason


def test_decide_xargs_deny_match():
    d = decide_bash("echo x | xargs touch", _rules(deny=["Bash(touch:*)"]))
    assert d.verdict == "ask"
    assert "Bash(touch:*)" in d.reason


def test_decide_sudo_with_option_escalates():
    # `sudo -u root` leaves a leading flag we don't model — opaque, so ask
    # rather than let the real command slip through unchecked.
    d = decide_bash("sudo -u root touch /tmp/x", _rules(deny=["Bash(touch:*)"]))
    assert d.verdict == "ask"
    assert "opaque wrapper option" in d.reason


# ----------------------------------------------------------------------------
# Variable-substituted command name — hidden from tripwires AND deny matching.
# ----------------------------------------------------------------------------

def test_decide_variable_command_name_asks_without_deny():
    # `$C` hides which program runs from the raw-string tripwires; escalate
    # even with no deny rules configured.
    d = decide_bash("C=touch; $C /tmp/x", _rules())
    assert d.verdict == "ask"
    assert "variable" in d.reason


def test_decide_variable_command_name_asks_with_deny():
    d = decide_bash("$C /tmp/x", _rules(deny=["Bash(rm:*)"]))
    assert d.verdict == "ask"
    assert "variable" in d.reason


def test_decide_variable_command_inside_sh_c():
    d = decide_bash("bash -c '$C evil'", _rules())
    assert d.verdict == "ask"
    assert "variable" in d.reason


@pytest.mark.parametrize("command", ["echo $HOME", "cat $FILE", "cd $DIR"])
def test_decide_variable_in_args_not_flagged(command):
    # A variable as an *argument* is fine — the command name is literal.
    assert decide_bash(command, _rules()).verdict == "pass"


# ----------------------------------------------------------------------------
# _rule_matches — bare Bash, malformed rule, exact (non-prefix) match.
# ----------------------------------------------------------------------------

def test_rule_matches_bare_bash_matches_everything():
    assert _rule_matches("Bash", "rm -rf /") is True


def test_rule_matches_non_bash_rule_is_false():
    assert _rule_matches("Read(/etc/*)", "cat /etc/passwd") is False


def test_rule_matches_exact_form():
    assert _rule_matches("Bash(ls)", "ls") is True
    assert _rule_matches("Bash(ls)", "ls -la") is False


# ----------------------------------------------------------------------------
# _deny_atom — empty atom skipped; non-matching c-wrapper falls through.
# ----------------------------------------------------------------------------

def test_deny_atom_skips_assignment_only_atom():
    # `FOO=bar` strips to an empty argv and must not crash or match.
    assert _deny_atom("FOO=bar", ["Bash(rm:*)"]) == ""


def test_deny_atom_c_wrapper_no_match_falls_through():
    # The inner command is parsed but doesn't match the deny rule.
    assert _deny_atom("bash -c 'git status'", ["Bash(rm:*)"]) == ""


# ----------------------------------------------------------------------------
# decide_edit / make_edit_path_hook — confine edit targets to the project root.
# ----------------------------------------------------------------------------

def test_edit_target_picks_first_present_key():
    assert _edit_target({"file_path": "/a"}) == "/a"
    assert _edit_target({"notebook_path": "/b"}) == "/b"
    assert _edit_target({"path": "/c"}) == "/c"
    assert _edit_target({"file_path": "", "path": "/d"}) == "/d"
    assert _edit_target({"other": "/e"}) is None


def test_decide_edit_in_project_passes(tmp_path):
    base = str(tmp_path)
    d = decide_edit({"file_path": str(tmp_path / "src" / "a.py")}, base)
    assert d.verdict == "pass"


def test_decide_edit_notebook_in_project_passes(tmp_path):
    d = decide_edit({"notebook_path": str(tmp_path / "nb.ipynb")}, str(tmp_path))
    assert d.verdict == "pass"


def test_decide_edit_dotdot_escape_asks(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    target = str(proj / ".." / "outside" / "x.py")
    d = decide_edit({"file_path": target}, str(proj))
    assert d.verdict == "ask"
    assert "escapes project root" in d.reason


def test_decide_edit_symlink_escape_asks(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (proj / "link").symlink_to(outside)
    d = decide_edit({"file_path": str(proj / "link" / "x.py")}, str(proj))
    assert d.verdict == "ask"


def test_decide_edit_no_target_defers():
    # No path key → nothing to confine; let the settings layer decide.
    assert decide_edit({}, "/tmp/proj").verdict == "pass"


@pytest.mark.parametrize("sub", [".git/hooks/pre-commit", ".claude/settings.json"])
def test_decide_edit_protected_subdir_asks(tmp_path, sub):
    # In-tree but inside .git/.claude — auto-allowing these lets a silent edit
    # plant a git hook or inject allow rules, so escalate to the phone.
    d = decide_edit({"file_path": str(tmp_path / sub)}, str(tmp_path))
    assert d.verdict == "ask"
    assert "protected" in d.reason


def test_decide_edit_normal_in_project_still_passes(tmp_path):
    d = decide_edit({"file_path": str(tmp_path / "src" / "a.py")}, str(tmp_path))
    assert d.verdict == "pass"


def test_decide_edit_dotdot_back_into_git_asks(tmp_path):
    proj = tmp_path / "proj"
    (proj / "x").mkdir(parents=True)
    target = str(proj / "x" / ".." / ".git" / "hooks" / "p")
    d = decide_edit({"file_path": target}, str(proj))
    assert d.verdict == "ask"
    assert "protected" in d.reason


# ----------------------------------------------------------------------------
# decide_bash protected-write — always-on guard for writes into .git/.claude,
# independent of deny config (survives a future relaxation of redirect gating).
# ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "echo x > .git/hooks/pre-commit",
        "echo x >> .claude/settings.json",
        "cat > ./.git/config",
        "echo x > sub/../.git/hooks/p",
        "cmd &> .claude/x",
        "cmd >| .git/x",
    ],
)
def test_decide_bash_protected_write_asks(tmp_path, command):
    d = decide_bash(command, _rules(), str(tmp_path))
    assert d.verdict == "ask"
    assert "protected" in d.reason


def test_decide_bash_protected_write_ignores_deny_config(tmp_path):
    # Same verdict with deny empty or populated — it does not rely on the
    # deny-gated escape-kind path.
    cmd = "echo x > .git/hooks/p"
    assert decide_bash(cmd, _rules(), str(tmp_path)).verdict == "ask"
    assert decide_bash(cmd, _rules(deny=["Bash(rm:*)"]), str(tmp_path)).verdict == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "echo x > note.txt",       # ordinary write
        "cat < .git/config",       # read, not a write
        "cat .git/config",         # plain read, no redirect
        "cmd 2>&1",                # fd dup
        "cmd > /dev/null",         # bit bucket
    ],
)
def test_decide_bash_protected_write_passes_safe(tmp_path, command):
    assert decide_bash(command, _rules(), str(tmp_path)).verdict == "pass"


@pytest.mark.parametrize(
    "command",
    [
        "echo evil | tee .git/hooks/pre-commit",   # tee positional
        "tee -a .claude/settings.json",            # tee append flag + target
        "cp evil .git/hooks/pre-commit",           # cp destination
        "cp -t .git/hooks evil",                   # cp -t target-directory form
        "mv evil .claude/settings.json",           # mv destination
        "install -m755 x .git/hooks/post-merge",   # install destination
        "dd if=payload of=.git/hooks/pre-push",    # dd of= operand
        "sudo cp evil .git/hooks/pre-commit",      # transparent wrapper stripped
        "cd .git/hooks && echo evil > pre-commit",  # cd-relative redirect
        "cd .claude && cp /tmp/e settings.json",   # cd-relative writer arg
    ],
)
def test_decide_bash_protected_writer_command_asks(tmp_path, command):
    # Writes into .git/.claude via a command argument (not a `>` redirect) or
    # via a relative path after `cd` into the subtree are escalated too.
    d = decide_bash(command, _rules(), str(tmp_path))
    assert d.verdict == "ask"
    assert "protected" in d.reason


@pytest.mark.parametrize(
    "command",
    [
        "cp a.py src/b.py",            # in-tree copy, nothing protected
        "tee out.log",                 # ordinary tee target
        "mv old.txt new.txt",
        "dd if=/dev/zero of=disk.img bs=1M",
        "cd src && cp x.py y.py",      # cd into a normal subdir
    ],
)
def test_decide_bash_protected_writer_command_passes_safe(tmp_path, command):
    assert decide_bash(command, _rules(), str(tmp_path)).verdict == "pass"


def test_decide_bash_no_project_path_skips_protected():
    # Back-compat: without a project root the protected check is skipped.
    assert decide_bash("echo x > .git/hooks/p", _rules()).verdict == "pass"


async def test_bash_hook_protected_write_uses_project_root(tmp_path):
    hook = make_bash_permission_hook(_rules(), str(tmp_path))
    data = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo x > .git/hooks/p"},
    }
    out = await hook(data, None, {})
    assert _verdict(out) == "ask"
    assert "protected" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_decide_edit_unresolvable_path_asks(monkeypatch):
    # A resolution failure (e.g. symlink loop) is treated as suspicious.
    def boom(self, *a, **k):
        raise OSError("loop")

    monkeypatch.setattr("pathlib.Path.resolve", boom)
    d = decide_edit({"file_path": "/whatever"}, "/tmp/proj")
    assert d.verdict == "ask"
    assert "resolution failed" in d.reason


async def test_edit_hook_non_edit_tool_returns_empty():
    hook = make_edit_path_hook("/tmp/proj")
    assert await hook(_input("Bash", "ls"), None, {}) == {}


async def test_edit_hook_in_project_returns_empty(tmp_path):
    hook = make_edit_path_hook(str(tmp_path))
    data = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "a.py")}}
    assert await hook(data, None, {}) == {}


async def test_edit_hook_escape_returns_ask(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    hook = make_edit_path_hook(str(proj))
    data = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(proj / ".." / "outside.txt")},
    }
    out = await hook(data, None, {})
    assert _verdict(out) == "ask"
    assert "phone approval required" in out["hookSpecificOutput"][
        "permissionDecisionReason"
    ]


# ----------------------------------------------------------------------------
# _redirect_write_target / _protected_write_reason — defensive branches that
# real bashlex output can't reach (a `>&`/`<&` with a non-word output is caught
# by `_benign_redirect` first), exercised directly with stub nodes / patched
# path resolution.
# ----------------------------------------------------------------------------

def test_redirect_write_target_non_word_output_is_none():
    # A non-benign redirect (type `>`, not `>&`/`<&`) whose output carries no
    # `.kind == "word"` yields no write target. bashlex never emits this — a
    # plain `>` always points at a word — so drive it with a stub node.
    class _Stub:
        pass

    node = _Stub()
    node.type = ">"
    node.output = 1  # an int, not a WordNode — getattr(.., "kind") is None
    assert _redirect_write_target(node) is None


def test_protected_write_unparseable_stays_permissive():
    # A parse failure must not block: the guard returns "" and the command
    # falls through to the layers below.
    assert _protected_write_reason("echo ' unbalanced", "/tmp/proj") == ""


def test_protected_write_guarded_resolve_failure_stays_permissive(monkeypatch):
    # If resolving the .git/.claude guard dirs themselves blows up, stay
    # permissive rather than crash the hook.
    def boom(self, *a, **k):
        raise RuntimeError("resolve exploded")

    monkeypatch.setattr("pathlib.Path.resolve", boom)
    assert _protected_write_reason("echo x > out.txt", "/tmp/proj") == ""


def test_protected_write_target_resolve_failure_asks(monkeypatch):
    # Guard dirs resolve fine but the redirect target itself fails to resolve
    # (e.g. a symlink loop) — treated as suspicious, so escalate.
    orig = Path.resolve

    def selective(self, *a, **k):
        if "LOOPMARK" in str(self):
            raise OSError("symlink loop")
        return orig(self, *a, **k)

    monkeypatch.setattr("pathlib.Path.resolve", selective)
    reason = _protected_write_reason("echo x > LOOPMARK", "/tmp/proj")
    assert reason == "write path resolution failed"


# ----------------------------------------------------------------------------
# _writer_targets / _cd_targets — defensive branches not reachable through a
# realistic decide_bash path (assignment-only writer atom, flag-only `cd`,
# resolution failure on a cd target), exercised directly.
# ----------------------------------------------------------------------------

def test_writer_targets_empty_after_strip():
    # An atom that strips to nothing (a lone assignment) yields no targets
    # rather than indexing an empty argv.
    assert _writer_targets(["FOO=bar"]) == []


def test_cd_targets_skips_flag_only_cd(tmp_path):
    # `cd -P` carries no directory operand — it contributes no extra base.
    trees = bashlex.parse("cd -P && echo x > .git/hooks/p")
    assert _cd_targets(trees, tmp_path) == []


def test_cd_targets_resolution_failure_skipped(tmp_path, monkeypatch):
    # A cd destination that fails to resolve is skipped, not propagated.
    orig = Path.resolve

    def selective(self, *a, **k):
        if "LOOPDIR" in str(self):
            raise OSError("symlink loop")
        return orig(self, *a, **k)

    monkeypatch.setattr("pathlib.Path.resolve", selective)
    trees = bashlex.parse("cd LOOPDIR && echo x > p")
    assert _cd_targets(trees, tmp_path) == []


# ----------------------------------------------------------------------------
# Decision dataclass — sanity.
# ----------------------------------------------------------------------------

def test_decision_default_reason_empty():
    d = Decision("pass")
    assert d.reason == ""
