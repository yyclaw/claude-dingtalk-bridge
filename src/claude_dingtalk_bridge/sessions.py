from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path

from claude_agent_sdk import (
    get_session_info,
    list_sessions,
    project_key_for_directory,
)

_SUMMARY_LIMIT = 60

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


def format_size(num_bytes: int) -> str:
    """Render a byte count the way the TUI does: 1B / 37.9KB / 1.2MB."""
    if num_bytes < 1024:
        return f"{num_bytes}B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f}KB"
    return f"{num_bytes / (1024 * 1024):.1f}MB"


def format_tokens(n: int) -> str:
    """Render a token count compactly: 1.2K, 45K, 1.5M.

    Trailing zeros and a bare decimal point are stripped so 1000 renders as
    ``1K`` rather than ``1.0K``.
    """
    for limit, suffix in ((1_000_000, "M"), (1000, "K")):
        if n >= limit:
            return f"{n / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def is_uuid(text: str) -> bool:
    """True when text is a canonical UUID string."""
    try:
        return str(uuid.UUID(text)) == text.lower()
    except ValueError:
        return False


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


def format_session_list(infos, current_id: str | None) -> str:
    """Render a numbered, TUI-styled markdown list of recent sessions.

    Each entry is a bold title (the summary) followed by a metadata line
    `time · branch · size`, mirroring the desktop TUI. The leading index
    is kept so the phone can reply with `/resume <n>`.
    """
    blocks = [f"📋 **Recent sessions ({len(infos)})**"]
    for idx, info in enumerate(infos, start=1):
        summary = (info.summary or "(no summary)").replace("\n", " ").strip()
        if len(summary) > _SUMMARY_LIMIT:
            summary = summary[:_SUMMARY_LIMIT] + "…"
        summary = md_escape(summary)
        meta = [format_relative_time(info.last_modified)]
        if info.git_branch:
            meta.append(md_escape(info.git_branch))
        meta.append(format_size(info.file_size))
        if info.session_id == current_id:
            meta.append("⭐ current")
        blocks.append(f"**{idx}. {summary}**  \n{' · '.join(meta)}")
    blocks.append("💬 `/resume <n>` to switch")
    return "\n\n".join(blocks)


def session_transcript_path(project_path: str, session_id: str) -> Path:
    """Filesystem path of the SDK-maintained JSONL transcript for a session.

    Mirrors the CLI/SDK convention: ``~/.claude/projects/<project-key>/<sid>.jsonl``.
    The path is computed, not stat'd — call sites can present it to the user
    even before the file exists.
    """
    key = project_key_for_directory(project_path)
    return Path.home() / ".claude" / "projects" / key / f"{session_id}.jsonl"


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
    sessions.py free of a module-load dependency on log_context."""
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


async def list_recent_sessions(project_path: str, limit: int):
    """Recent sessions for a project, newest first (SDK file-IO off-thread)."""
    return await asyncio.to_thread(
        list_sessions, directory=project_path, limit=limit
    )


async def find_session(project_path: str, session_id: str):
    """Look up one session by id within a project; None if not found."""
    return await asyncio.to_thread(
        get_session_info, session_id, directory=project_path
    )
