"""Per-turn log context: stamps every log line emitted during a turn with the
session id and turn number, so a single grep can slice one session's full
trace out of multi-session/multi-project log streams.

Implemented with contextvars so values propagate across asyncio await
boundaries automatically — log call sites stay unchanged.
"""
from __future__ import annotations

import contextvars

import time

_session: contextvars.ContextVar[str] = contextvars.ContextVar(
    "log_session", default="-"
)
_turn: contextvars.ContextVar[str] = contextvars.ContextVar(
    "log_turn", default="-"
)
# Project working directory for the current turn. Read by tool-input path
# collapsing so paths inside the project render as relative (./foo/bar) and
# paths under $HOME render with `~/`. Empty string disables collapsing.
_cwd: contextvars.ContextVar[str] = contextvars.ContextVar(
    "log_cwd", default=""
)
# In-flight tool uses: tool_use_id → (start_time, tool_name). Populated when
# an AssistantMessage emits a ToolUseBlock, consumed when the matching
# UserMessage tool_result lands — lets the result line surface (a) which
# tool actually returned (current log only shows the id) and (b) wall-clock
# duration of the tool call. Default is None so .clear() can install a
# fresh dict; each consumer creates one lazily via _tool_use_map.
_tool_uses: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "log_tool_uses", default=None
)
# In-flight subagents: tool_use_id (of the Task tool that spawned the
# subagent) → (task_id, subagent_type). Populated on TaskStartedMessage when
# subagent_type is set (background Bash also fires task_started but is not a
# subagent and never produces child assistant messages — skipped). Consumed
# by lookup_subagent() from AssistantMessage/UserMessage where
# parent_tool_use_id matches, so child lines can show sub_id + sub_type
# alongside the existing parent_tool_use_id without forcing the reader to
# eye-correlate task_started lines.
_subagents: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "log_subagents", default=None
)

_SUBAGENT_ID_PREFIX_LEN = 8

# UUID prefixes of this length are unique enough within recent history while
# keeping every log line short. Matches what the Claude transcripts on disk
# already key by (the leading segment of the session uuid).
_SESSION_PREFIX_LEN = 8


def set_session(session_id: str | None) -> None:
    """Set the session label for the current task's log lines.

    Pass the full UUID; only the leading prefix is kept for display.
    Passing None clears it back to ``-``.
    """
    _session.set(session_id[:_SESSION_PREFIX_LEN] if session_id else "-")


def set_turn(turn: int | None) -> None:
    """Set the turn label for the current task's log lines."""
    _turn.set(str(turn) if turn is not None else "-")


def set_cwd(cwd: str | None) -> None:
    """Set the project working directory for path-collapsing in tool previews."""
    _cwd.set(cwd or "")


def _tool_use_map() -> dict:
    """Lazy-init a fresh dict for the in-flight tool-use map. ContextVar
    default of None means each turn starts with its own dict (set here on
    first read), avoiding cross-turn id collisions."""
    d = _tool_uses.get()
    if d is None:
        d = {}
        _tool_uses.set(d)
    return d


def record_tool_use(tool_id: str, tool_name: str) -> None:
    """Note that a tool call started — pairs with take_tool_use() at result
    time to recover (name, elapsed)."""
    if not tool_id:
        return
    _tool_use_map()[tool_id] = (time.monotonic(), tool_name)


def take_tool_use(tool_id: str) -> tuple[str | None, float | None]:
    """Pop and return (tool_name, elapsed_seconds) for a tool_use id.

    Returns (None, None) when the id wasn't recorded — e.g. a tool_result
    arriving after a turn boundary, or a subagent's tool whose tool_use
    came on a prior message we didn't observe.
    """
    if not tool_id:
        return None, None
    entry = _tool_use_map().pop(tool_id, None)
    if entry is None:
        return None, None
    start, name = entry
    return name, time.monotonic() - start


def _subagent_map() -> dict:
    """Lazy-init a fresh dict for the in-flight subagent map. Same pattern as
    _tool_use_map — None default lets clear() install a fresh dict per turn
    without leaking cross-turn ids."""
    d = _subagents.get()
    if d is None:
        d = {}
        _subagents.set(d)
    return d


def record_subagent(tool_use_id: str | None, task_id: str, subagent_type: str | None) -> None:
    """Note that a subagent (Task tool with a subagent_type) has started.

    Skipped silently when ``tool_use_id`` or ``subagent_type`` is missing —
    bare background Bash also emits task_started but is not a subagent and
    has nothing to tag.
    """
    if not tool_use_id or not subagent_type:
        return
    _subagent_map()[tool_use_id] = (task_id, subagent_type)


def forget_subagent(tool_use_id: str | None) -> None:
    """Drop a subagent entry on task_notification. No-op when missing."""
    if not tool_use_id:
        return
    _subagent_map().pop(tool_use_id, None)


def lookup_subagent(tool_use_id: str | None) -> tuple[str | None, str | None]:
    """Return (short_task_id, subagent_type) for the subagent whose Task
    tool call has the given id, or (None, None) if unknown.

    Lookup misses are expected at restart boundaries (task_started arrived in
    a previous daemon process) — callers should still print agent=sub when
    parent_tool_use_id alone proves subagent origin.
    """
    if not tool_use_id:
        return None, None
    entry = _subagent_map().get(tool_use_id)
    if entry is None:
        return None, None
    task_id, sub_type = entry
    return task_id[:_SUBAGENT_ID_PREFIX_LEN], sub_type


def clear() -> None:
    """Reset all labels — call between unrelated tasks sharing a context."""
    _session.set("-")
    _turn.set("-")
    _cwd.set("")
    # Fresh dicts — don't leak in-flight tool ids or subagent entries from a
    # previous turn.
    _tool_uses.set({})
    _subagents.set({})


def session_label() -> str:
    return _session.get()


def turn_label() -> str:
    return _turn.get()


def cwd_label() -> str:
    return _cwd.get()
