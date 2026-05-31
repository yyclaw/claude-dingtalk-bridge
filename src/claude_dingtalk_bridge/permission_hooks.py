"""The PreToolUse permission hooks the bridge installs on the SDK.

Each hook is deliberately one-sided: it only ever upgrades a tool call to
``ask`` (phone approval), never hard-denies. Two hooks are installed.

**Bash hook** (:func:`make_bash_permission_hook`). Sources that trigger an
upgrade, in order:

1. **Built-in tripwires** — literal patterns for catastrophic operations
   (``rm -f``/``rm -rf`` including force split across flag groups, ``dd
   of=/dev/…``, ``mkfs``, redirect to a block device, classic fork bomb).
   Always on, no config knob. Matched twice: once on the raw string (a coarse
   backstop, also covering parse failures and the structural fork bomb) and
   once on the *parsed* command (:func:`_parsed_tripwire`), which normalizes
   away quoting (``"rm" -rf``), a path prefix (``/bin/rm``) and split-word
   tricks (``mk''fs``) before matching. Still evadable by a runtime expansion
   that hides a flag (``$IFS``, ``$'\\x2d\\x72\\x66'``), encoding, or an
   interpreter wrapper — those need other defenses. False positives become one
   extra phone ask.

2. **Variable-substituted command name** (:func:`_variable_command`) and
   **writes into the project's ``.git``/``.claude``** (:func:`_protected_write_reason`).
   Both run unconditionally (no deny rule needed). The first escalates ``$CMD``
   in command position, which hides the program from every other layer; the
   second escalates a write resolving into a protected subdir so a silent
   ``> .git/hooks/pre-commit`` can't plant a hook. The write guard covers
   ``>``/``>>`` redirect targets, file-writer command operands
   (``cp``/``mv``/``install``/``tee`` positionals, ``dd of=``), and any of those
   reached by a relative path after a ``cd`` into the subtree. A write whose
   path is built from a runtime expansion, or hidden inside an interpreter/
   encoding wrapper, is still out of scope, like the tripwires.

3. **User-configured ``deny`` rules** — only consulted when the user actually
   set any. The command is parsed with bashlex so a deny on ``rm:*`` catches
   ``cd /tmp && rm -rf foo`` even though the surface command doesn't start
   with ``rm``. A path-prefixed invocation is matched on its basename too, so
   ``rm:*`` also catches ``/bin/rm`` / ``./rm``; both ``sh -c "…"`` and
   ``eval "…"`` are recursed into, so a deny target hidden in either is found.
   Unparseable input / unsupported constructs degrade to ask, as does a
   transparent wrapper that leaves option flags we don't model (e.g.
   ``exec -a NAME``, ``command -p``, ``env -i``) since the real command can no
   longer be matched against the deny prefix.

**Edit-path hook** (:func:`make_edit_path_hook`). Edit-shaped tools are auto-
allowed inside the project root by the settings layer's ``Edit(<cwd>/**)``
glob, but that match is textual — a ``..`` segment or an in-tree symlink can
slip past it. This hook resolves the target and escalates any edit whose
resolved path lands outside the project root, or inside a protected subdir
(``.git``/``.claude``) where an in-tree edit could plant a hook or inject
allow rules.

Everything else returns ``{}``: the SDK's settings layer (allow rules) plus
``permission_mode`` get to decide. With no deny rules the Bash hook still runs
the tripwires, the variable-command guard and the protected-write guard.

PreToolUse hooks run before the CLI's settings-layer permission resolution,
so an ask returned from here cannot be short-circuited by an allow rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import bashlex

from claude_dingtalk_bridge.config import PermissionRules

# bashlex node kinds that hide a sub-command we can't statically inspect.
# When deny rules are configured and we see one of these, escalate to ask
# rather than let the command through unchecked.
_ESCAPE_KINDS: frozenset[str] = frozenset({
    "commandsubstitution", "processsubstitution", "redirect", "heredoc",
    "compound", "if", "for", "while", "until", "case", "function",
})

# Prefixes that wrap another command without changing which command runs.
# Stripping them exposes the real command to deny matching, so `exec rm`
# is treated exactly like `rm`. `sudo`/`xargs` are included so `sudo curl` and
# `… | xargs curl` get matched against the deny list rather than slipping past
# it; when either is given its own option flags (`sudo -u`, `xargs -0`) the
# leading dash makes the atom opaque and the deny path escalates to ask.
_SIMPLE_TRANSPARENT: frozenset[str] = frozenset({
    "exec", "command", "builtin", "nohup", "setsid", "sudo", "xargs",
})
_ARG_TRANSPARENT: frozenset[str] = frozenset({
    "nice", "ionice", "stdbuf", "timeout", "caffeinate",
})
_SHELLS: frozenset[str] = frozenset({"bash", "sh", "zsh", "dash"})

# Project subdirectories that carry code-execution / permission authority — a
# git hook or a `.claude/settings.json` allow rule. Writing into them is auto-
# allowed by neither hook: both the edit-path hook and the Bash write guard
# escalate any in-tree target landing here to the phone.
_PROTECTED_DIRS: frozenset[str] = frozenset({".git", ".claude"})

# Commands that write a file named as an *argument* rather than via a `>`
# redirect. Their positional operands are resolved against the project root the
# same way redirect targets are, so a write into `.git`/`.claude` through
# `cp`/`tee`/`mv`/`install`/`dd` is escalated even with no redirect present.
_FILE_WRITERS: frozenset[str] = frozenset({"cp", "mv", "install", "tee"})

_BLOCK_DEV_RE = re.compile(r"^/dev/(?:sd[a-z]|nvme\d|disk\d|rdisk\d)")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_NUM_RE = re.compile(r"^\d+[smhd]?$")
# A short-flag group passing `-c` to a shell: the `c` may sit anywhere in the
# group (`-c`, `-lc`, `-cl`, `-cx`) — bash reads the command from the next
# argument regardless of flag order, so anchoring `c` to the end missed `-cl`.
_C_FLAG_RE = re.compile(r"^-[a-z]*c[a-z]*$")

# Built-in catastrophic-literal tripwires. Run on the raw command string —
# coarse, regex-based, evadable. Their job is to catch the "operator wrote
# out the dangerous form in the clear and an allowlist would have nodded it
# through". Smart bypasses (interpreter wrappers, encoding) are out of scope
# for this layer; the deny-rule path handles wrappers it can decompose.
_TRIPWIRES: tuple[tuple[re.Pattern[str], str], ...] = (
    # `rm` whose force flag appears in any short-flag group — whether bundled
    # (`-rf`, `-fr`, `-Rf`, `-vrf`) or split across groups (`rm -r -f`,
    # `rm --recursive -f`). Intermediate groups (long or short) are skipped so
    # the `f` group can sit anywhere after `rm`. Plain `rm -r` with no force at
    # all is still not caught — configure a deny rule if you want that too.
    (
        re.compile(r"\brm\s+(?:-+[a-zA-Z]*\s+)*-[a-zA-Z]*[fF][a-zA-Z]*\b"),
        "rm -f",
    ),
    # `rm --force` (long form).
    (
        re.compile(r"\brm\b.*?--force\b", re.DOTALL),
        "rm --force",
    ),
    # `dd` writing to a device node — corrupts disks / partitions.
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/"), "dd of=/dev/*"),
    # Formats / wipes a filesystem.
    (re.compile(r"\bmkfs(?:\.[a-z0-9]+)?\b"), "mkfs"),
    # Redirect into a raw block device.
    (
        re.compile(r">\s*/dev/(?:sd[a-z]|nvme\d|disk\d|rdisk\d)"),
        "write to block device",
    ),
    # Classic fork bomb.
    (
        re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:?\s*&\s*\}\s*;\s*:"),
        "fork bomb",
    ),
)


Hook = Callable[[dict[str, Any], str | None, dict[str, Any]], Awaitable[dict]]


@dataclass
class Decision:
    verdict: str  # "ask" | "pass"
    reason: str = ""


def _basename(arg: str) -> str:
    """Drop a leading path so ``/bin/rm`` and ``rm`` normalize alike.

    bashlex has already stripped quotes and backslash escapes from the word, so
    ``"rm"`` / ``'rm'`` / ``r"m"`` / ``\\rm`` all arrive as ``rm``; this only has
    to remove a directory prefix.
    """
    return arg.rsplit("/", 1)[-1]


def _parsed_tripwire(command: str) -> str:
    """Re-run the literal tripwires against the *parsed* command.

    The raw-string pass in :func:`tripwire_match` is defeated by quoting
    (``"rm" -rf``), a path prefix (``/bin/rm -rf``) or split-word tricks
    (``mk''fs``) — the regex sees characters the shell would have collapsed.
    Here each command atom is rebuilt from bashlex's already-unquoted words
    (program name reduced to its basename) and the same patterns are matched
    against that normalized line; write-redirect targets are checked for a raw
    block device (so ``> "/dev/sda"`` is caught too). Residual gaps: an inline
    expansion the shell would resolve at runtime — ``$IFS`` field-splitting or
    ``$'\\x2d\\x72\\x66'`` ANSI-C quoting — still hides a flag, since bashlex
    keeps it as an opaque literal. A parse failure yields ``""`` and the raw
    pass remains the backstop.
    """
    try:
        trees = bashlex.parse(command)
    except Exception:  # noqa: BLE001 — raw pass already ran as the backstop
        return ""
    for tree in trees:
        for node in _walk(tree):
            kind = getattr(node, "kind", None)
            if kind == "command":
                argv = _argv(node)
                if not argv:
                    continue
                line = " ".join([_basename(argv[0]), *argv[1:]])
                for pattern, label in _TRIPWIRES:
                    if pattern.search(line):
                        return label
            elif kind == "redirect":
                target = _redirect_write_target(node)
                if target and _BLOCK_DEV_RE.match(target):
                    return "write to block device"
    return ""


def tripwire_match(command: str) -> str:
    """Return a label if ``command`` matches a built-in dangerous literal.

    Two passes: the coarse regexes on the raw string (a backstop that also
    covers parse failures and structural forms like the fork bomb), then a
    parsed pass (:func:`_parsed_tripwire`) that normalizes away quoting / path /
    split-word evasions before matching.
    """
    for pattern, label in _TRIPWIRES:
        if pattern.search(command):
            return label
    return _parsed_tripwire(command)


def _benign_redirect(node) -> bool:
    """True for a redirect that hides no sub-command and writes nothing real.

    Covers fd duplications/closes (``2>&1``, ``>&2``, ``>&-``) and redirects to
    ``/dev/null`` (``> /dev/null``, ``2>/dev/null``, ``< /dev/null``). bashlex
    gives an fd-duplication an *integer* ``output`` (the target fd) and a
    file/heredoc redirect a ``WordNode``. ``>&`` with a WordNode is the bash
    "both streams to file" form — a real write, so not benign.
    """
    rtype = getattr(node, "type", None)
    output = getattr(node, "output", None)
    is_word = getattr(output, "kind", None) == "word"
    if rtype in (">&", "<&") and not is_word:
        return True
    return is_word and output.word == "/dev/null"


def _redirect_write_target(node) -> str | None:
    """Return the file path a redirect *writes* to, or ``None``.

    ``None`` for benign redirects (see :func:`_benign_redirect`), fd-only
    targets (``2>&1``), and read redirects (``<``, ``<<``, ``<<<``, ``<&``).
    Write forms (``>``, ``>>``, ``>|``, ``&>``, and ``>&`` to a file word) yield
    the target word. Used by the protected-dir write guard.
    """
    if _benign_redirect(node):
        return None
    output = getattr(node, "output", None)
    if getattr(output, "kind", None) != "word":
        return None
    rtype = getattr(node, "type", None) or ""
    if rtype.startswith("<"):
        return None
    return output.word


def _escape_reason(trees) -> str:
    """Return a reason if parsed ``trees`` contain an opaque construct.

    Subshells, substitutions, file redirects, heredocs and compound statements
    hide a sub-command the atom matcher can't inspect, so the deny path treats
    them as ``ask``. Benign redirects (fd dups, ``/dev/null``) are exempt. Kept
    separate so a future relaxation of redirect gating is a one-function edit.
    """
    for tree in trees:
        for node in _walk(tree):
            kind = getattr(node, "kind", None)
            if kind == "redirect" and _benign_redirect(node):
                continue
            if kind in _ESCAPE_KINDS:
                return f"unsupported construct ({kind})"
    return ""


def _walk(node):
    yield node
    for child in getattr(node, "parts", None) or []:
        yield from _walk(child)
    sub = getattr(node, "command", None)
    if sub is not None and hasattr(sub, "kind"):
        yield from _walk(sub)
    for child in getattr(node, "list", None) or []:
        if hasattr(child, "kind"):
            yield from _walk(child)
    for attr in ("output", "input"):
        child = getattr(node, attr, None)
        if child is not None and hasattr(child, "kind"):
            yield from _walk(child)


def _argv(command_node) -> list[str]:
    return [
        p.word for p in command_node.parts if getattr(p, "kind", None) == "word"
    ]


def _strip_transparent(argv: list[str]) -> list[str]:
    argv = list(argv)
    changed = True
    while changed and argv:
        changed = False
        while argv and _ASSIGN_RE.match(argv[0]):
            argv.pop(0)
            changed = True
        if not argv:
            break
        head = argv[0]
        if head in _SIMPLE_TRANSPARENT:
            argv.pop(0)
            changed = True
        elif head == "env":
            argv.pop(0)
            changed = True
            while argv and _ASSIGN_RE.match(argv[0]):
                argv.pop(0)
        elif head in _ARG_TRANSPARENT:
            argv.pop(0)
            changed = True
            while argv and (argv[0].startswith("-") or _NUM_RE.match(argv[0])):
                argv.pop(0)
    return argv


def _c_wrapper_inner(argv: list[str]) -> str | None:
    if not argv or argv[0] not in _SHELLS:
        return None
    for i, tok in enumerate(argv[1:], start=1):
        if _C_FLAG_RE.match(tok):
            return argv[i + 1] if i + 1 < len(argv) else None
    return None


def _eval_inner(argv: list[str]) -> str | None:
    """Return the command string an ``eval`` runs, or ``None`` if not ``eval``.

    ``eval`` concatenates its arguments and runs the result as a command, so it
    is recursed exactly like ``sh -c`` for deny matching and variable-command
    detection — closing the ``bash -c '…'`` ↔ ``eval '…'`` asymmetry. The
    concatenation may be empty (``eval`` with no args); that recurses harmlessly
    to no atoms. basename-normalized so a path form is handled like ``rm``.
    """
    if not argv or _basename(argv[0]) != "eval":
        return None
    return " ".join(argv[1:])


def _nested_inner(argv: list[str]) -> str | None:
    """Inner command of a wrapper that runs a string: ``sh -c`` or ``eval``."""
    inner = _c_wrapper_inner(argv)
    return inner if inner is not None else _eval_inner(argv)


def _rule_matches(rule: str, cmdstr: str) -> bool:
    if rule == "Bash":
        return True
    if not (rule.startswith("Bash(") and rule.endswith(")")):
        return False
    inner = rule[len("Bash(") : -1]
    if inner.endswith(":*"):
        prefix = inner[:-2]
        return cmdstr == prefix or cmdstr.startswith(prefix + " ")
    return cmdstr == inner


def _matched_deny_rule(argv: list[str], deny_rules: list[str]) -> str:
    """Return the first deny rule matching ``argv``, or ``""``.

    Matches the joined argv against each rule's prefix. When the program is
    invoked by path (``/bin/rm``, ``./rm``), the basename form is tried too, so
    ``Bash(rm:*)`` catches ``/bin/rm`` while an explicit ``Bash(/bin/rm:*)``
    still matches the path form. The extra candidate is built only when a slash
    is present, so slash-free commands (``go run``) are unaffected.
    """
    candidates = [" ".join(argv)]
    if argv and "/" in argv[0]:
        candidates.append(" ".join([_basename(argv[0]), *argv[1:]]))
    for rule in deny_rules:
        if any(_rule_matches(rule, c) for c in candidates):
            return rule
    return ""


def _deny_atom(command: str, deny_rules: list[str]) -> str:
    """Return a reason if any atom in ``command`` matches a user deny rule.

    The reason is self-describing so the caller can surface it verbatim: a real
    hit reads ``matches deny rule <rule>``, while a conservative escalation names
    its own cause (``unparseable command``, ``unsupported construct (…)``,
    ``opaque wrapper option``) rather than masquerading as a rule match.

    Unparseable input and escape constructs (subshell, file redirect,
    heredoc, …) are treated as opaque and trigger an ask, since we can't tell
    whether the hidden sub-command would have matched. Benign redirects (fd
    duplications and ``/dev/null``, see ``_benign_redirect``) are exempt.
    """
    try:
        trees = bashlex.parse(command)
    except Exception:  # noqa: BLE001 — parse trouble degrades to a phone ask
        return "unparseable command"
    escape = _escape_reason(trees)
    if escape:
        return escape
    atoms = [
        node
        for tree in trees
        for node in _walk(tree)
        if getattr(node, "kind", None) == "command"
    ]
    for atom in atoms:
        argv = _strip_transparent(_argv(atom))
        if not argv:
            continue
        if argv[0].startswith("-"):
            # A transparent wrapper left option flags we don't model — e.g.
            # `exec -a NAME cmd`, `command -p cmd`, `env -i cmd`, `env -u VAR
            # cmd`. The real command is now ambiguous (an arg-taking flag like
            # `-a NAME` would mis-anchor the deny prefix match), so escalate
            # rather than risk a false pass.
            return "opaque wrapper option"
        inner = _nested_inner(argv)
        if inner is not None:
            nested = _deny_atom(inner, deny_rules)
            if nested:
                return nested
            continue
        matched = _matched_deny_rule(argv, deny_rules)
        if matched:
            return f"matches deny rule {matched}"
    return ""


def _variable_command(command: str) -> str:
    """Return a reason if a command name is itself a variable expansion.

    ``$CMD …`` / ``${CMD} …`` hide which program runs from both the raw-string
    tripwires and the deny matcher (bashlex can't resolve the value), so this
    runs unconditionally — even with no deny rules. Only the command *name* is
    inspected; a variable as an argument (``rm $FILE``, ``echo $HOME``) is left
    alone. A parse failure returns ``""`` so the empty-deny auto-mode stays
    permissive (the deny path re-parses and degrades to ask when configured).
    """
    try:
        trees = bashlex.parse(command)
    except Exception:  # noqa: BLE001 — empty-deny stays permissive on parse fail
        return ""
    atoms = [
        node
        for tree in trees
        for node in _walk(tree)
        if getattr(node, "kind", None) == "command"
    ]
    for atom in atoms:
        argv = _strip_transparent(_argv(atom))
        if not argv or argv[0].startswith("-"):
            continue
        inner = _nested_inner(argv)
        if inner is not None:
            nested = _variable_command(inner)
            if nested:
                return nested
            continue
        if "$" in argv[0]:
            return "variable-substituted command name"
    return ""


def _writer_targets(argv: list[str]) -> list[str]:
    """Return file paths a writer command targets, or ``[]``.

    Coarse on purpose. For ``cp``/``mv``/``install``/``tee`` every positional
    (non-flag) operand is returned — both the destination and any sources — so
    a trailing ``DEST`` and a ``-t DIR`` target-directory form are both covered;
    the cost is that *reading* from a protected dir (``cp .git/config …``) also
    escalates, which is an acceptable extra phone ask. ``dd`` is matched on its
    ``of=`` operand. Transparent wrappers (``sudo cp …``) are stripped first.
    """
    argv = _strip_transparent(argv)
    if not argv:
        return []
    cmd = _basename(argv[0])
    rest = argv[1:]
    if cmd == "dd":
        return [a[len("of="):] for a in rest if a.startswith("of=")]
    if cmd in _FILE_WRITERS:
        return [a for a in rest if not a.startswith("-")]
    return []


def _cd_targets(trees, base: Path) -> list[Path]:
    """Resolved directories named by any ``cd`` atom, as extra write bases.

    ``cd .git/hooks && echo x > pre-commit`` writes into ``.git`` even though the
    redirect target is the bare ``pre-commit``. Collecting ``cd`` destinations as
    additional resolution bases catches that without fully emulating the shell's
    working directory across the chain.
    """
    out: list[Path] = []
    for tree in trees:
        for node in _walk(tree):
            if getattr(node, "kind", None) != "command":
                continue
            argv = _strip_transparent(_argv(node))
            if not argv or _basename(argv[0]) != "cd":
                continue
            dirs = [a for a in argv[1:] if not a.startswith("-")]
            if not dirs:
                continue
            try:
                out.append((base / Path(dirs[0]).expanduser()).resolve())
            except (OSError, RuntimeError):
                continue
    return out


def _protected_hit(target: str, bases: list[Path], guarded: list[Path]) -> str:
    """Reason if ``target`` resolves into a guarded dir under any base, else ``""``.

    A resolution failure is treated as suspicious (escalate), matching the
    redirect-target behaviour the guard had before it grew extra bases.
    """
    for base in bases:
        try:
            resolved = (base / Path(target).expanduser()).resolve()
        except (OSError, RuntimeError):
            return "write path resolution failed"
        if any(resolved.is_relative_to(g) for g in guarded):
            return "writes into protected dir (.git/.claude or ~/.claude)"
    return ""


def _protected_write_reason(command: str, project_path: str) -> str:
    """Return a reason if ``command`` writes into a protected dir.

    Guarded dirs are the project's own ``.git``/``.claude`` *and* the user's
    home ``~/.claude`` — the latter is the global Claude Code control plane
    (``settings.json``'s ``defaultMode``/``allow``, ``CLAUDE.md``, hooks), so a
    write there could rewrite the approval model for every project at once. Edit/
    Write tools already escalate any out-of-root target via :func:`decide_edit`;
    this closes the matching Bash path, which otherwise falls through to the
    settings layer where a future ``Bash(cp:*)`` allow (or the auto classifier)
    could wave a ``cp``/``mv`` into ``~/.claude`` through.

    Runs unconditionally (independent of the deny list and of the deny-gated
    redirect escalation), so a write that plants a git hook or rewrites a
    ``settings.json`` always reaches the phone — even if redirect gating is
    relaxed later. Covered write surfaces: ``>``/``>>`` redirect targets,
    file-writer command operands (``cp``/``mv``/``install``/``tee`` positionals,
    ``dd of=``), and any of those reached via a relative path after a ``cd``
    into a guarded subtree (``cd``-targets join the resolution bases). A literal
    ``~`` in the target is expanded; still out of scope, same as the tripwires:
    a write whose path is built from a runtime expansion (``$HOME``), or an
    interpreter/encoding wrapper that hides the write entirely. A parse failure
    stays permissive so auto-mode isn't broken.
    """
    try:
        trees = bashlex.parse(command)
    except Exception:  # noqa: BLE001 — stay permissive on parse trouble
        return ""
    base = Path(project_path).expanduser()
    try:
        guarded = [(base / d).resolve() for d in _PROTECTED_DIRS]
        guarded.append((Path.home() / ".claude").resolve())
    except (OSError, RuntimeError):
        return ""
    bases = [base, *_cd_targets(trees, base)]
    for tree in trees:
        for node in _walk(tree):
            kind = getattr(node, "kind", None)
            if kind == "redirect":
                targets = [_redirect_write_target(node)]
            elif kind == "command":
                targets = _writer_targets(_argv(node))
            else:
                continue
            for target in targets:
                if target is None:
                    continue
                reason = _protected_hit(target, bases, guarded)
                if reason:
                    return reason
    return ""


def decide_bash(
    command: str,
    rules: PermissionRules | None,
    project_path: str | None = None,
) -> Decision:
    """Decide whether the hook should escalate ``command`` to phone ask.

    Returns ``ask`` on a built-in tripwire match, a variable-substituted command
    name, a write into the project's ``.git``/``.claude`` (when ``project_path``
    is given) — all checked unconditionally since they hide intent from every
    other layer — or a user deny rule match (when any are configured). Otherwise
    returns ``pass``: the SDK settings layer plus ``permission_mode`` decide.
    """
    label = tripwire_match(command)
    if label:
        return Decision("ask", f"dangerous literal ({label})")
    var = _variable_command(command)
    if var:
        return Decision("ask", var)
    if project_path is not None:
        protected = _protected_write_reason(command, project_path)
        if protected:
            return Decision("ask", protected)
    if rules is None or not rules.deny:
        return Decision("pass")
    reason = _deny_atom(command, rules.deny)
    if reason:
        return Decision("ask", reason)
    return Decision("pass")


def _output(decision: str, reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


def make_bash_permission_hook(
    rules: PermissionRules | None, project_path: str | None = None
) -> Hook:
    """Build the PreToolUse Bash hook bound to the rules and project root.

    ``project_path`` is the active turn's project root; it enables the
    protected-dir write guard. Omitted (``None``) the guard is skipped.
    """

    async def hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict:
        if input_data.get("tool_name") != "Bash":
            return {}
        command = (input_data.get("tool_input") or {}).get("command") or ""
        if not command:
            return {}
        decision = decide_bash(command, rules, project_path)
        if decision.verdict == "ask":
            return _output("ask", decision.reason + "; phone approval required.")
        return {}

    return hook


# Edit-shaped tools and the input keys that carry their target path.
_EDIT_TOOLS: frozenset[str] = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit"}
)
_EDIT_PATH_KEYS: tuple[str, ...] = ("file_path", "notebook_path", "path")


def _edit_target(tool_input: dict[str, Any]) -> str | None:
    for key in _EDIT_PATH_KEYS:
        value = tool_input.get(key)
        if value:
            return value
    return None


def decide_edit(tool_input: dict[str, Any], cwd: str) -> Decision:
    """Decide whether an edit-tool call should escalate to phone ask.

    The settings layer auto-allows ``Edit(<cwd>/**)`` by *textual* glob, which
    a ``..`` segment or an in-tree symlink can slip past — the literal path
    matches the glob yet resolves outside the project. Resolving both sides and
    comparing closes that hole. A resolution failure (e.g. a symlink loop) is
    treated as suspicious and escalated. An in-tree target that lands inside a
    protected subdir (``.git``/``.claude``) is escalated too — auto-allowing it
    would let a silent edit plant a git hook or inject allow rules. Any other
    target resolving inside the project root returns ``pass``, letting the allow
    rule auto-approve the in-project edit without a phone prompt.
    """
    target = _edit_target(tool_input)
    if target is None:
        return Decision("pass")
    try:
        resolved = Path(target).expanduser().resolve()
        base = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        return Decision("ask", "edit path resolution failed")
    if not resolved.is_relative_to(base):
        return Decision("ask", "edit target escapes project root")
    relative = resolved.relative_to(base)
    if relative.parts and relative.parts[0] in _PROTECTED_DIRS:
        return Decision("ask", "edit targets protected dir (.git/.claude)")
    return Decision("pass")


def make_edit_path_hook(project_path: str) -> Hook:
    """Build the PreToolUse hook that confines edit-tool targets to ``project_path``."""

    async def hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict:
        if input_data.get("tool_name") not in _EDIT_TOOLS:
            return {}
        decision = decide_edit(input_data.get("tool_input") or {}, project_path)
        if decision.verdict == "ask":
            return _output("ask", decision.reason + "; phone approval required.")
        return {}

    return hook
