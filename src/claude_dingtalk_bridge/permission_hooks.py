"""The PreToolUse permission hook the daemon installs on the SDK.

One Bash hook is installed. It hard-denies a small fixed set of
catastrophic literals plus a sibling guard; everything else returns ``{}``
and the SDK's settings layer (allow/deny rules) plus ``permission_mode``
get to decide. Sources that trigger a hard deny, in order:

1. **Built-in tripwires** — literal patterns for catastrophic operations
   (``rm -f``/``rm -rf`` including force split across flag groups, ``find …
   -delete``, ``dd of=/dev/…``, ``newfs_*``, ``diskutil`` destructive verbs
   incl. ``apfs delete{Container,Volume}`` (macOS disk wipe), ``asr restore``,
   ``gpt destroy``/``remove``, redirect to a block device, classic fork bomb).
   Always on, no config knob. Two sets: ``_STRUCTURAL_TRIPWIRES`` (fork bomb)
   run on the raw string always, since the AST flattens the fork bomb to a
   benign ``:`` call; ``_ATOM_TRIPWIRES`` run via the parsed pass
   (:func:`_parsed_tripwire`), which normalizes away quoting (``"rm" -rf``), a
   path prefix (``/bin/rm``), split-word tricks (``mk''fs``), and **recurses
   into ``bash -c`` / ``eval`` wrappers** so a buried ``rm -rf`` is caught. When
   bashlex can't parse the input, the atom regexes get one raw-string pass as a
   fallback. Still evadable by a runtime expansion that hides a flag (``$IFS``,
   ``$'\\x2d\\x72\\x66'``), encoding, or an opaque interpreter wrapper — those
   need other defenses. False positives become one extra phone deny.

2. **Variable-substituted command name** (:func:`_variable_command`).
   Escalates ``$CMD`` / ``${CMD}`` in command position (including inside
   ``sh -c`` / ``eval``), which hides the program from the tripwire regexes
   and from every settings-layer matcher.

Hook returns ``permissionDecision: "deny"`` because that is the only
verdict that hard-blocks across every ``permission_mode`` — verified across
``default`` / ``auto`` / ``bypassPermissions`` (see
``docs/permission-empirical-matrix.md``). An ``ask`` is silently overridable
by the ``auto``-mode in-CLI classifier and skipped by ``bypassPermissions``.

Path-level deny rules (``Bash(rm:*)``, ``Edit(<path>/**)``) are NOT handled
here — the user configures those in Claude Code's own settings layers
(``~/.claude/settings.json``, project ``.claude/settings.json``).

PreToolUse hooks run before the CLI's settings-layer permission resolution,
so a deny returned from here cannot be undone by an allow rule in any layer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import bashlex

# Prefixes that wrap another command without changing which command runs.
# Stripping them exposes the real program to the tripwire/variable-command
# checks, so `exec rm -rf` is inspected exactly like `rm -rf`. `sudo`/`xargs`
# are included so `sudo rm -rf` and `… | xargs rm` are reached too.
_SIMPLE_TRANSPARENT: frozenset[str] = frozenset({
    "exec", "command", "builtin", "nohup", "setsid", "sudo", "xargs",
})
_ARG_TRANSPARENT: frozenset[str] = frozenset({
    "nice", "ionice", "stdbuf", "timeout", "caffeinate",
})
_SHELLS: frozenset[str] = frozenset({"bash", "sh", "zsh", "dash"})

_BLOCK_DEV_RE = re.compile(r"^/dev/(?:sd[a-z]|nvme\d|disk\d|rdisk\d)")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_NUM_RE = re.compile(r"^\d+[smhd]?$")
# A short-flag group passing `-c` to a shell: the `c` may sit anywhere in the
# group (`-c`, `-lc`, `-cl`, `-cx`) — bash reads the command from the next
# argument regardless of flag order, so anchoring `c` to the end missed `-cl`.
_C_FLAG_RE = re.compile(r"^-[a-z]*c[a-z]*$")

# Atom-level tripwires — matched against each parsed command atom (and recursed
# into through `bash -c` / `eval`). Each is also rerun on the raw string when
# bashlex fails to parse, so an unparseable but obviously dangerous literal
# still trips.
_ATOM_TRIPWIRES: tuple[tuple[re.Pattern[str], str], ...] = (
    # `rm` whose force flag appears in any short-flag group — whether bundled
    # (`-rf`, `-fr`, `-Rf`, `-vrf`) or split across groups (`rm -r -f`,
    # `rm --recursive -f`). Intermediate groups (long or short) are skipped so
    # the `f` group can sit anywhere after `rm`. Plain `rm -r` with no force at
    # all is still not caught.
    (
        re.compile(r"\brm\s+(?:-+[a-zA-Z]*\s+)*-[a-zA-Z]*[fF][a-zA-Z]*\b"),
        "rm -f",
    ),
    # `rm --force` (long form).
    (
        re.compile(r"\brm\b.*?--force\b", re.DOTALL),
        "rm --force",
    ),
    # `find … -delete` — same destructive footprint as `rm -rf` but no `rm`
    # atom. `\s-delete\b` keeps a literal filename like `foo-delete` from
    # tripping (no whitespace right before `-delete` in that case).
    (re.compile(r"\bfind\b[^\n]*\s-delete\b"), "find -delete"),
    # `dd` writing to a device node — corrupts disks / partitions.
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/"), "dd of=/dev/*"),
    # Formats a filesystem (macOS `newfs_hfs` / `newfs_apfs` / `newfs_msdos`…).
    (re.compile(r"\bnewfs_[a-z0-9]+\b"), "newfs"),
    # macOS disk eraser — `diskutil` with a destructive verb wipes a disk,
    # volume, or APFS container. Read-only verbs (`list`, `info`) are
    # unaffected. APFS `deleteContainer`/`deleteVolume` are the apfs-subcommand
    # forms that the legacy verb list missed.
    (
        re.compile(
            r"\bdiskutil\b[^\n]*\b(?:eraseDisk|eraseVolume|reformat|"
            r"zeroDisk|randomDisk|secureErase|partitionDisk|"
            r"deleteContainer|deleteVolume)\b"
        ),
        "diskutil erase",
    ),
    # macOS Apple Software Restore — `asr restore` writes an image to the
    # target disk/volume, overwriting it. Read-only verbs (`imagescan`,
    # `verify`) are unaffected.
    (re.compile(r"\basr\b[^\n]*\brestore\b"), "asr restore"),
    # macOS `gpt(8)` — `destroy` wipes the GPT header, `remove` deletes a
    # partition entry. Read-only verbs (`show`) are unaffected.
    (re.compile(r"\bgpt\b[^\n]*\b(?:destroy|remove)\b"), "gpt destroy"),
)

# Redirect into a raw block device. In the parsed pass this is caught by the
# redirect-node branch in `_tripwire_in_trees` (which inspects the redirect
# target via `_BLOCK_DEV_RE`); this raw-string form is only needed by the
# parse-failure fallback, where there is no AST to walk.
_BLOCK_DEV_REDIRECT_RE = re.compile(r">\s*/dev/(?:sd[a-z]|nvme\d|disk\d|rdisk\d)")

# Structural tripwires — no parsed atom rebuilds carry their signature, so they
# run on the raw command string always. Today this is just the fork bomb; the
# AST flattens it to a benign-looking `:` call.
_STRUCTURAL_TRIPWIRES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Classic fork bomb.
    (
        re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:?\s*&\s*\}\s*;\s*:"),
        "fork bomb",
    ),
)


Hook = Callable[[dict[str, Any], str | None, dict[str, Any]], Awaitable[dict]]


@dataclass
class Decision:
    verdict: str  # "deny" | "pass"
    reason: str = ""


def _basename(arg: str) -> str:
    """Drop a leading path so ``/bin/rm`` and ``rm`` normalize alike.

    bashlex has already stripped quotes and backslash escapes from the word, so
    ``"rm"`` / ``'rm'`` / ``r"m"`` / ``\\rm`` all arrive as ``rm``; this only has
    to remove a directory prefix.
    """
    return arg.rsplit("/", 1)[-1]


def _tripwire_in_trees(trees) -> str:
    """Match atom-level tripwires against already-parsed ``trees``.

    For each command atom we rebuild a basename-normalized argv string and
    match it against ``_ATOM_TRIPWIRES``. When the atom is a `bash -c "…"` /
    `eval "…"` wrapper, the inner command string is re-parsed and walked the
    same way, so a buried ``rm -rf`` is caught. Write-redirect targets are
    additionally checked for a raw block-device path.
    """
    for tree in trees:
        for node in _walk(tree):
            kind = getattr(node, "kind", None)
            if kind == "command":
                argv = _strip_transparent(_argv(node))
                if not argv:
                    continue
                inner = _nested_inner(argv)
                if inner is not None:
                    nested = _parsed_tripwire(inner)
                    if nested:
                        return nested
                    continue
                line = " ".join([_basename(argv[0]), *argv[1:]])
                for pattern, label in _ATOM_TRIPWIRES:
                    if pattern.search(line):
                        return label
            elif kind == "redirect":
                target = _redirect_write_target(node)
                if target and _BLOCK_DEV_RE.match(target):
                    return "write to block device"
    return ""


def _parsed_tripwire(command: str) -> str:
    """Parse ``command`` and match atom-level tripwires; ``""`` on parse failure.

    A thin parse-and-walk wrapper around :func:`_tripwire_in_trees`, used to
    recurse into the inner string of a `bash -c "…"` / `eval "…"` wrapper.
    """
    try:
        trees = bashlex.parse(command)
    except Exception:  # noqa: BLE001 — caller falls back to raw-string regex
        return ""
    return _tripwire_in_trees(trees)


def _raw_tripwire_fallback(command: str) -> str:
    """Coarse raw-string scan, used only when bashlex can't parse ``command``.

    With no AST to walk, the atom regexes (and the block-device redirect form)
    run against the raw string so an unparseable but obvious literal still trips.
    """
    for pattern, label in _ATOM_TRIPWIRES:
        if pattern.search(command):
            return label
    if _BLOCK_DEV_REDIRECT_RE.search(command):
        return "write to block device"
    return ""


def tripwire_match(command: str) -> str:
    """Return a label if ``command`` matches a built-in catastrophic literal.

    Order: structural raw-string patterns (fork bomb) → atom-level parsed pass
    (which recurses into ``bash -c`` / ``eval``). When bashlex can't parse the
    input, the raw-string fallback runs instead so an obvious literal still trips.
    """
    for pattern, label in _STRUCTURAL_TRIPWIRES:
        if pattern.search(command):
            return label
    try:
        trees = bashlex.parse(command)
    except Exception:  # noqa: BLE001 — parse failed → raw fallback path
        return _raw_tripwire_fallback(command)
    return _tripwire_in_trees(trees)


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
    the target word. Used by the tripwire's block-device redirect check.
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
    is recursed exactly like ``sh -c`` for the tripwire and variable-command
    checks — closing the ``bash -c '…'`` ↔ ``eval '…'`` asymmetry. The
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


def _variable_in_trees(trees) -> str:
    """Return a reason if a command name in ``trees`` is a variable expansion."""
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


def _variable_command(command: str) -> str:
    """Return a reason if a command name is itself a variable expansion.

    ``$CMD …`` / ``${CMD} …`` hide which program runs from the raw-string
    tripwires and from every settings-layer matcher (bashlex can't resolve the
    value). Only the command *name* is inspected; a variable as an argument
    (``rm $FILE``, ``echo $HOME``) is left alone. A parse failure returns ``""``
    so a command bashlex can't parse stays permissive here (the tripwire's raw
    fallback is the backstop).
    """
    try:
        trees = bashlex.parse(command)
    except Exception:  # noqa: BLE001 — stays permissive on parse failure
        return ""
    return _variable_in_trees(trees)


def decide_bash(command: str) -> Decision:
    """Decide whether the hook should hard-deny ``command``.

    Returns ``deny`` on a built-in tripwire match or a variable-substituted
    command name; otherwise returns ``pass``: the SDK settings layer plus
    ``permission_mode`` decide. The command is parsed with bashlex once and the
    tree is shared by both checks (a raw-string fallback covers parse failures).
    """
    for pattern, label in _STRUCTURAL_TRIPWIRES:
        if pattern.search(command):
            return Decision("deny", f"dangerous literal ({label})")
    try:
        trees = bashlex.parse(command)
    except Exception:  # noqa: BLE001 — parse failed → raw fallback path
        label = _raw_tripwire_fallback(command)
        if label:
            return Decision("deny", f"dangerous literal ({label})")
        return Decision("pass")
    label = _tripwire_in_trees(trees)
    if label:
        return Decision("deny", f"dangerous literal ({label})")
    var = _variable_in_trees(trees)
    if var:
        return Decision("deny", var)
    return Decision("pass")


def _output(decision: str, reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


def make_bash_permission_hook() -> Hook:
    """Build the PreToolUse Bash hook."""

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
        decision = decide_bash(command)
        if decision.verdict == "deny":
            return _output(
                "deny", f"blocked by daemon safety guard: {decision.reason}"
            )
        return {}

    return hook
