"""Phone- and log-friendly rendering helpers.

These functions turn raw values (bytes, tokens, timestamps, filesystem paths,
free-form text containing paths) into the short, human-readable form used in
phone messages and log lines. They have no session-specific dependencies and
are reused across the codebase — keep them small and pure.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

# Strip the trailing `-YYYYMMDD` datestamp some Anthropic model IDs carry
# (e.g. `claude-haiku-4-5-20251001`). The base ID is what users recognize;
# the date is dead weight on a phone status line.
_MODEL_DATE_SUFFIX = re.compile(r"-\d{8}(?=\[|$)")

# After prefix/datestamp stripping, the only remaining digit-dash-digit run
# is the major/minor version (e.g. `opus-4-7`). Render it as `opus-4.7` so a
# phone reader doesn't parse the hyphen as a range.
_MODEL_VERSION_DASH = re.compile(r"(\d)-(\d)")

# Numeric HTML entities for markdown-significant chars. The markdown parser
# sees the entity (not the glyph) so it triggers no emphasis/code/link/tag;
# the HTML stage decodes it back. '&' goes first so emitted entities are not
# themselves re-escaped.
_MD_ENTITIES = {
    "&": "&#38;",
    "<": "&#60;",
    ">": "&#62;",
    "*": "&#42;",
    "_": "&#95;",
    "`": "&#96;",
    "[": "&#91;",
    "]": "&#93;",
}


def md_escape(text: str) -> str:
    """Escape user/Claude-supplied text for literal rendering in markdown."""
    for ch, ent in _MD_ENTITIES.items():
        text = text.replace(ch, ent)
    return text


# Empty heading carrying a full-width space: DingTalk's markdown renderer
# collapses ordinary blank lines, so this forces a visible gap between blocks.
MD_SPACER = "###### 　"


def format_size(num_bytes: int) -> str:
    """Render a byte count the way the TUI does: 1B / 37.9KB / 1.2MB."""
    if num_bytes < 1024:
        return f"{num_bytes}B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f}KB"
    return f"{num_bytes / (1024 * 1024):.1f}MB"


def short_model_name(model: str) -> str:
    """Phone-friendly form of an Anthropic model id.

    Drops the redundant ``claude-`` prefix and any ``-YYYYMMDD`` datestamp,
    preserving suffixes like ``[1m]`` that denote a meaningful variant.
    """
    name = model.removeprefix("claude-")
    name = _MODEL_DATE_SUFFIX.sub("", name)
    return _MODEL_VERSION_DASH.sub(r"\1.\2", name)


def format_cost(cost: float) -> str:
    """Render a USD cost compactly: $0.42, $22.40, $100.00.

    Anything below one cent collapses to ``<$0.01`` so a nearly-free turn
    doesn't read as ``$0.00`` (which looks like a stale or unfilled field).
    """
    if cost < 0.01:
        return "<$0.01"
    return f"${cost:.2f}"


def format_tokens(n: int) -> str:
    """Render a token count compactly: 1.2K, 45K, 1.5M.

    Trailing zeros and a bare decimal point are stripped so 1000 renders as
    ``1K`` rather than ``1.0K``.
    """
    for limit, suffix in ((1_000_000, "M"), (1000, "K")):
        if n >= limit:
            return f"{n / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def format_uptime(seconds: int) -> str:
    """Render an elapsed duration as ``X days Y hours Z minutes``.

    Sub-minute durations collapse to ``just now``; zero-valued components are
    dropped (so a clean day reads ``1 day``, not ``1 day 0 hours 0 minutes``)
    and units are singular/plural to match their count.
    """
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    parts = []
    for value, unit in ((days, "day"), (hours, "hour"), (mins, "minute")):
        if value:
            parts.append(f"{value} {unit}" + ("s" if value != 1 else ""))
    return " ".join(parts)


def format_relative_time(epoch_ms: int, now_ms: int | None = None) -> str:
    """Render an epoch-ms timestamp as a phone-friendly relative time."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    seconds = max(0, now - epoch_ms) // 1000
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    days = seconds // 86400
    if days == 1:
        return "yesterday"
    return f"{days}d ago"


# Cached once: $HOME is process-constant and _collapse_home runs on every
# log line at INFO. Calling os.path.expanduser per line repeats a pwd/env
# lookup for no reason.
_HOME = os.path.expanduser("~")


def _collapse_home(s: str) -> str:
    """Replace any ``$HOME/`` prefix occurrences in s with ``~/``.

    Internal — used as the second step of :func:`collapse_inline_paths` and
    :func:`display_path`. Not exported because callers should always go
    through one of those (which also handle the project-relative step).
    """
    if s and _HOME and _HOME != "/":
        s = s.replace(_HOME + "/", "~/")
    return s


def _current_cwd() -> str:
    """Read the active turn's cwd from log_context. Local import keeps
    display.py free of a module-load dependency on log_context."""
    from claude_dingtalk_bridge import log_context
    return log_context.cwd_label()


def collapse_inline_paths(s: str, cwd: str | None = None) -> str:
    """Shorten every absolute path embedded inside a free-form string.

    Phone messages and log lines should be readable, not buried in absolute
    paths. The rule, applied in order:

    1. Paths under the current project root render relative (``/proj/src/x``
       → ``src/x``; a bare ``/proj`` token → ``.``).
    2. Paths under ``$HOME`` (outside the project) render with ``~/`` prefix.

    "Inline" is the contract: ``s`` is text that *contains* paths (Bash
    commands, tool previews, log lines), not a single path. The matching is
    plain substring substitution — fast and good enough for typical command
    text, but it can mangle paths that share a prefix with cwd at non-path
    boundaries (e.g. cwd=``/proj`` would rewrite ``/projstuff/x`` to
    ``.stuff/x``). For a single whole path use :func:`display_path` instead,
    which is path-boundary-aware. Passing ``cwd`` overrides log_context —
    useful in unit tests or when formatting outside the turn loop.
    """
    if not s:
        return s
    if cwd is None:
        cwd = _current_cwd()
    if cwd:
        # `cwd + "/"` → "" so a path like `/proj/src/x.py` becomes `src/x.py`.
        # Bare `cwd` → "." so `find /proj -name …` becomes `find . -name …`.
        s = s.replace(cwd + "/", "")
        s = s.replace(cwd, ".")
    return _collapse_home(s)


def display_path(path: Path | str, cwd: str | None = None) -> str:
    """Render a single whole filesystem path via the same two-step rule as
    :func:`collapse_inline_paths`, but with path-boundary-aware matching.

    Project-internal paths render as relative; other home-prefixed paths
    render with ``~``; paths outside both stay absolute. Unlike
    :func:`collapse_inline_paths`, the project check uses ``startswith(cwd +
    "/")`` so a path like ``/projstuff/x`` (which only shares a prefix with
    cwd ``/proj``) is left alone — correct for whole-path inputs. Passing
    ``cwd`` overrides the log_context lookup.
    """
    s = str(path)
    if cwd is None:
        cwd = _current_cwd()
    if cwd:
        if s == cwd:
            return "."
        if s.startswith(cwd + "/"):
            return s[len(cwd) + 1:]
    if s == _HOME:
        return "~"
    if _HOME and _HOME != "/" and s.startswith(_HOME + "/"):
        return "~" + s[len(_HOME):]
    return s
