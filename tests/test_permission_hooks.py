import bashlex
import pytest

from claude_dingtalk_bridge.permission_hooks import (
    Decision,
    _benign_redirect,
    _c_wrapper_inner,
    _eval_inner,
    _redirect_write_target,
    _strip_transparent,
    _variable_command,
    _walk,
    decide_bash,
    make_bash_permission_hook,
    tripwire_match,
)


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


@pytest.mark.parametrize(
    "command",
    [
        # bash -c wrapping a force-flag rm — the inner atom must be inspected.
        'bash -c "rm -rf /tmp/x"',
        "sh -c 'rm -rf /tmp/x'",
        'bash -lc "rm -r -f /tmp/x"',           # short-flag group with c not last
        # cd then bash -c (chain + wrap) — outer chain irrelevant, inner is the danger.
        "cd /tmp && bash -c 'rm -rf foo'",
        # eval form — the joined args become the inner command.
        "eval 'rm -rf /tmp/y'",
        "eval rm -rf /tmp/y",
        # nested wraps: bash -c inside eval.
        '''eval "bash -c 'rm -rf /tmp/z'"''',
        # dd through a wrap.
        "sh -c 'dd if=/dev/zero of=/dev/sda bs=1M'",
        # newfs through a wrap.
        'bash -c "newfs_hfs /dev/disk2"',
    ],
)
def test_tripwire_recurses_into_wrap_forms(command):
    # Surface coverage for catastrophic ops hidden behind `bash -c '…'` /
    # `eval '…'`. Note most of these cases would also pass WITHOUT recursion:
    # the inner `rm`/`newfs`/`dd` is unquoted, so the dangerous literal is
    # visible to the raw-string pass on the full command. They're kept for
    # per-case surface coverage and clearer failure messages, not as a
    # recursion guard — `test_tripwire_recursion_catches_what_raw_pass_cannot`
    # below is the actual guard (inner program quoted so only recursion catches
    # it).
    assert tripwire_match(command) != ""


def test_tripwire_parse_failure_falls_back_to_raw_regex(monkeypatch):
    # If bashlex blows up on the input, we still want the raw-string regex
    # pass to catch the obvious literal — otherwise an unparseable but clearly
    # dangerous command silently passes.
    import claude_dingtalk_bridge.permission_hooks as ph

    def _boom(_):
        raise RuntimeError("synthetic parse failure")

    monkeypatch.setattr(ph.bashlex, "parse", _boom)
    assert ph.tripwire_match("rm -rf /tmp/x") == "rm -f"


def test_tripwire_fork_bomb_still_caught_via_raw_pass():
    # Fork bomb is structural (recursive function def + call); no single atom
    # carries its signature. Keep it covered via the raw-string pass.
    assert tripwire_match(":(){ :|:& };:") == "fork bomb"


@pytest.mark.parametrize(
    "command",
    [
        # Inner program quoted so the raw rm-force pattern can't match it on the
        # full command, but the parsed recursion unquotes it: only the recursion
        # catches these.
        '''bash -c '"rm" -rf /tmp/x' ''',
        '''sh -c "'rm' -rf /tmp/x"''',
        # eval with a quoted inner program.
        '''eval '"rm" -rf /tmp/y' ''',
    ],
)
def test_tripwire_recursion_catches_what_raw_pass_cannot(command):
    # These are the cases that justify the recursion. The rm-force pattern
    # `\brm\s+...[fF]...` misses `"rm" -rf` on the raw outer string because the
    # closing quote breaks the `\brm\s+` anchor (a bare `\brm\b` WOULD match —
    # `"` is a non-word char — but the full force pattern needs `rm` followed by
    # whitespace then the flag group, which the quote interrupts). A
    # *non-recursive* parsed pass also misses (the only outer atom is
    # `bash`/`sh`/`eval`). Only the recursive pass, which re-parses and unquotes
    # the inner string to plain `rm -rf`, catches them. If recursion regresses
    # these fail while the tautological wrap tests stay green. (The `/bin/rm`
    # path-prefix form is deliberately excluded — the raw force pattern matches
    # `/bin/rm -rf` since the boundary sits between `/` and `rm`, so it is not a
    # recursion-only guard.)
    assert tripwire_match(command) != ""


# ----------------------------------------------------------------------------
# Built-in tripwires — fire unconditionally, no config.
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
        "newfs_hfs /dev/disk2",
        "newfs_apfs -v Macintosh /dev/disk3",
        "diskutil eraseDisk APFS Untitled /dev/disk2",
        "diskutil eraseVolume free none /dev/disk3s2",
        "diskutil zeroDisk /dev/disk4",
        # apfs-subcommand erasers the legacy verb list missed.
        "diskutil apfs deleteContainer disk2",
        "diskutil apfs deleteVolume disk2s1",
        # `find … -delete` — `rm -rf` without the `rm`.
        "find /tmp/x -delete",
        "find /tmp -name '*.tmp' -delete",
        "find . -type d -empty -delete",
        # macOS Apple Software Restore overwriting a disk/volume.
        "asr restore --source /tmp/x.dmg --target /dev/disk2 --erase --noprompt",
        # macOS gpt(8) destructive verbs.
        "gpt destroy /dev/disk2",
        "gpt remove -i 1 /dev/disk2",
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
        "diskutil list",            # read-only diskutil verbs are safe
        "diskutil info /dev/disk0",
        "find /tmp -name '*.log'",  # find without -delete is fine
        "asr imagescan --source x.dmg",  # read-only asr verb
        "asr verify --source x.dmg",
        "gpt show /dev/disk0",      # read-only gpt verb
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
        "new''fs_hfs /dev/disk2",   # split-word newfs
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
# decide_bash — "deny" on tripwire / variable-command, "pass" otherwise.
# ----------------------------------------------------------------------------

def test_decide_pass_when_safe():
    assert decide_bash("git status").verdict == "pass"


def test_decide_tripwire_denies():
    d = decide_bash("rm -rf /tmp/x")
    assert d.verdict == "deny"
    assert "rm -f" in d.reason


def test_decide_tripwire_unwraps_bash_c():
    # The tripwire recurses into bash -c, so a wrapped catastrophic literal
    # is denied even though the surface command is just `bash`.
    d = decide_bash("bash -c 'rm -rf /tmp/x'")
    assert d.verdict == "deny"


def test_decide_unparseable_passes():
    # An unparseable command with no dangerous literal falls through to the
    # settings layer — the tripwire's raw fallback found nothing.
    assert decide_bash("rm ' unbalanced").verdict == "pass"


def test_decide_fork_bomb_denies():
    # Structural tripwire: matched on the raw string before any parse, so it is
    # caught directly in decide_bash ahead of the bashlex pass.
    d = decide_bash(":(){ :|:& };:")
    assert d.verdict == "deny"
    assert "fork bomb" in d.reason


@pytest.mark.parametrize(
    ("command", "label"),
    [
        ("rm -rf /tmp/x", "rm -f"),
        ("newfs_hfs /dev/disk2", "newfs"),
        # Block-device redirect: the parsed path catches this via the redirect
        # node, but with no AST the raw fallback matches the `> /dev/...` form.
        ("cat foo > /dev/sda", "write to block device"),
    ],
)
def test_decide_raw_fallback_denies_when_parse_fails(monkeypatch, command, label):
    # When bashlex can't parse the input, decide_bash still denies an obvious
    # catastrophic literal through the raw-string fallback.
    import claude_dingtalk_bridge.permission_hooks as ph

    def _boom(_):
        raise RuntimeError("synthetic parse failure")

    monkeypatch.setattr(ph.bashlex, "parse", _boom)
    d = ph.decide_bash(command)
    assert d.verdict == "deny"
    assert label in d.reason


def test_decide_recurses_into_unparseable_inner_passes():
    # `bash -c` with an unparseable inner string: the outer parses, the
    # recursion into the inner returns "" on its own parse failure, and nothing
    # dangerous is found — so the call passes rather than raising.
    assert decide_bash("bash -c \"rm ' nope\"").verdict == "pass"


def test_decide_linux_mkfs_passes_intended_gap():
    # The bridge guards macOS disk wipers (newfs_* / diskutil erase); the
    # generic Linux `mkfs.ext4` family is intentionally NOT a tripwire (this is
    # a macOS daemon). Pin the conscious gap so the scope narrowing is explicit.
    assert decide_bash("mkfs.ext4 /dev/sdb1").verdict == "pass"


# ----------------------------------------------------------------------------
# awk / interpreter programs — a dangerous literal written in the clear inside
# the program string is still caught by the raw-string tripwire.
# ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "awk 'BEGIN{system(\"rm -rf ~/foo\")}'",
        "awk 'END{system(\"rm -fr /tmp/x\")}'",
    ],
)
def test_decide_awk_literal_rm_caught_by_tripwire(command):
    # The `rm -rf` literal inside the awk program is visible to the raw-string
    # tripwire even though `awk` is the only atom bashlex parses out.
    d = decide_bash(command)
    assert d.verdict == "deny"
    assert "rm -f" in d.reason


# ----------------------------------------------------------------------------
# make_bash_permission_hook — hook output mapping (deny / empty).
# ----------------------------------------------------------------------------

async def test_hook_non_bash_returns_empty():
    hook = make_bash_permission_hook()
    assert await hook(_input("Write", ""), None, {}) == {}


async def test_hook_empty_command_returns_empty():
    hook = make_bash_permission_hook()
    assert await hook(_input("Bash", ""), None, {}) == {}


async def test_hook_safe_command_returns_empty():
    hook = make_bash_permission_hook()
    assert await hook(_input("Bash", "git status"), None, {}) == {}


async def test_hook_tripwire_returns_deny():
    hook = make_bash_permission_hook()
    out = await hook(_input("Bash", "rm -rf ~/foo"), None, {})
    assert _verdict(out) == "deny"
    assert "rm -f" in out["hookSpecificOutput"]["permissionDecisionReason"]
    # The SDK validates this field before acting on permissionDecision.
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


async def test_hook_variable_command_returns_deny():
    hook = make_bash_permission_hook()
    out = await hook(_input("Bash", "$C /tmp/x"), None, {})
    assert _verdict(out) == "deny"
    assert "variable" in out["hookSpecificOutput"]["permissionDecisionReason"]
    # The SDK validates this field before acting on permissionDecision.
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


async def test_hook_node_eval_literal_rm_returns_deny():
    # The inner `rm -rf` IS visible to the raw-string tripwire — caught even
    # though `node` is the only atom bashlex parses out.
    hook = make_bash_permission_hook()
    out = await hook(
        _input(
            "Bash",
            "node -e 'require(\"child_process\").execSync(\"rm -rf /tmp/x\")'",
        ),
        None,
        {},
    )
    assert _verdict(out) == "deny"


async def test_hook_node_eval_no_literal_rm_passes():
    # Documented bypass: an interpreter call that hides even the literal `rm`
    # substring (`rmSync`) slips through — out of scope for the tripwire.
    hook = make_bash_permission_hook()
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
# _walk — full traversal recurses into nested .command / .list nodes. Used by
# the tripwire and variable-command passes; exercise the walker directly.
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
    kinds = _node_kinds("echo $(touch x)")
    assert "commandsubstitution" in kinds
    # The inner `touch` command node is reached via .command recursion.
    assert "command" in kinds


def test_walk_recurses_into_compound_list():
    # A compound node carries its body in `.list`.
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


def test_strip_transparent_strips_sudo():
    assert _strip_transparent(["sudo", "touch", "x"]) == ["touch", "x"]


def test_strip_transparent_strips_xargs():
    assert _strip_transparent(["xargs", "touch"]) == ["touch"]


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


def test_decide_cl_flag_order_unwraps_inner_tripwire():
    # Regression: `-cl` ordering must not slip a wrapped tripwire past the hook.
    d = decide_bash("bash -cl 'rm -rf /tmp/x'")
    assert d.verdict == "deny"


def test_eval_inner_joins_args():
    assert _eval_inner(["eval", "rm -r x"]) == "rm -r x"
    assert _eval_inner(["eval", "rm", "-r", "x"]) == "rm -r x"
    assert _eval_inner(["/bin/eval", "rm x"]) == "rm x"  # path form normalized
    assert _eval_inner(["eval"]) == ""                   # no args -> empty inner


def test_eval_inner_none_for_non_eval():
    assert _eval_inner(["bash", "-c", "x"]) is None
    assert _eval_inner([]) is None


# ----------------------------------------------------------------------------
# Variable-substituted command name — hidden from tripwires and settings rules.
# ----------------------------------------------------------------------------

def test_decide_variable_command_name_denies():
    # `$C` hides which program runs from the raw-string tripwires; deny.
    d = decide_bash("C=touch; $C /tmp/x")
    assert d.verdict == "deny"
    assert "variable" in d.reason


def test_decide_variable_command_inside_sh_c():
    d = decide_bash("bash -c '$C evil'")
    assert d.verdict == "deny"
    assert "variable" in d.reason


def test_decide_variable_command_inside_eval():
    # An eval'd command whose name is a variable is denied too, mirroring
    # `bash -c '$C evil'`.
    d = decide_bash("eval \"$C evil\"")
    assert d.verdict == "deny"
    assert "variable" in d.reason


@pytest.mark.parametrize("command", ["echo $HOME", "cat $FILE", "cd $DIR"])
def test_decide_variable_in_args_not_flagged(command):
    # A variable as an *argument* is fine — the command name is literal.
    assert decide_bash(command).verdict == "pass"


def test_variable_command_wrapper_inner_clean_continues():
    # A wrapper (`bash -c` / `eval`) whose inner command has a literal name
    # recurses, finds nothing, and continues past it — the outer `bash`/`eval`
    # atom is literal too, so the whole command is permissive.
    assert _variable_command("bash -c 'rm foo'") == ""
    assert _variable_command("eval 'echo hi'") == ""


def test_variable_command_unparseable_stays_permissive():
    # bashlex can't parse this, so the variable-command check has no atoms to
    # inspect and returns "" — the tripwire's raw fallback is the backstop.
    assert _variable_command("$C ' (") == ""


# ----------------------------------------------------------------------------
# Redirect handling inside the parsed tripwire pass — exercised through
# tripwire_match so the _walk → redirect → _redirect_write_target path runs.
# ----------------------------------------------------------------------------

def test_redirect_fd_duplication_is_benign():
    # `2>&1` is an fd duplication (non-word output) — benign, no write target,
    # so the parsed pass keeps scanning and finds nothing.
    assert _benign_redirect_via_parse("echo x 2>&1") is True
    assert tripwire_match("echo x 2>&1") == ""


def test_redirect_read_form_has_no_write_target():
    # A read redirect (`<`) writes nothing, so it never trips the block-device
    # check even with a real file word.
    assert tripwire_match("cat < /tmp/in") == ""


def test_redirect_write_to_regular_file_not_block_device():
    # A write redirect to an ordinary file yields a target that fails the
    # block-device match, so the scan continues past it (and recurses into the
    # target word node) without tripping.
    assert tripwire_match("cat foo > bar") == ""


def test_redirect_write_target_resolves_block_device():
    # The same path, but writing to a raw device, does trip.
    assert tripwire_match("cat foo > /dev/sda") == "write to block device"


def _benign_redirect_via_parse(command: str) -> bool:
    redirect = _first_redirect(command)
    return _benign_redirect(redirect)


def _first_redirect(command: str):
    for tree in bashlex.parse(command):
        for node in _walk(tree):
            if getattr(node, "kind", None) == "redirect":
                return node
    raise AssertionError(f"no redirect node in {command!r}")


def test_redirect_write_target_none_for_non_word_output():
    # Defensive guard: a non-benign redirect whose output isn't a word yields no
    # target. Real bashlex only emits non-word output for `>&`/`<&` (which
    # _benign_redirect already catches), so exercise it with a stub node.
    class _Stub:
        type = ">"
        output = 5  # an int fd, not a WordNode — kind is None
        input = None

    assert _redirect_write_target(_Stub()) is None


# ----------------------------------------------------------------------------
# Decision dataclass — sanity.
# ----------------------------------------------------------------------------

def test_decision_default_reason_empty():
    d = Decision("pass")
    assert d.reason == ""
