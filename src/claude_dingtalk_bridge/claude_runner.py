from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookEventMessage,
    MirrorErrorMessage,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from claude_dingtalk_bridge import log_context
from claude_dingtalk_bridge.display import collapse_inline_paths, display_path, format_tokens
from claude_dingtalk_bridge.questions import question_preview

logger = logging.getLogger(__name__)

# Hard upper bound on how long drain keeps the SDK client alive after a
# turn's ResultMessage. Only matters for subagents that never reach a
# terminal task_updated — i.e. truly stuck in the background. Acknowledged
# subagents short-circuit out via _SETTLE_TIMEOUT well before this fires.
_STUCK_TIMEOUT = 180.0

# Quiet-period drain exit. Once every pending subagent has been acknowledged
# (task_updated → completed/failed/stopped), the only thing drain might still
# capture is a straggler relay turn for one of them. If no new SDK message
# arrives for this long, the stream has settled and drain exits silently.
_SETTLE_TIMEOUT = 15.0

# Curated model list for the /model command. Mirrors the aliases the Claude
# CLI accepts for --model; only aliases (no version numbers) so it needn't
# change as models are bumped. Each entry is (alias, short description).
MODEL_CHOICES: list[tuple[str, str]] = [
    ("default", "recommended"),
    ("opus", "most capable"),
    ("sonnet", "balanced"),
    ("haiku", "fastest"),
]


def _short_tool_id(full: str | None) -> str | None:
    """Trim the constant `toolu_` prefix and keep the 8 chars that actually vary."""
    return full.removeprefix("toolu_")[:8] if full else None


def _cache_breakdown(usage: dict) -> dict:
    """Pull the prompt-cache numbers off an Anthropic usage dict.

    Every value is pre-formatted so the same dict can drive any log line or
    UI surface without each caller reinventing the format. ``hit`` is the
    prompt-side cache hit rate (cache_read / total prompt tokens), rendered
    as "N.N%" or "n/a" when no prompt tokens flowed (rare edge case). Shape
    is identical whether ``usage`` comes from a streaming AssistantMessage
    or the terminal ResultMessage.
    """
    cache_creation = usage.get("cache_creation") or {}
    read = usage.get("cache_read_input_tokens", 0)
    write_1h = cache_creation.get("ephemeral_1h_input_tokens", 0)
    write_5m = cache_creation.get("ephemeral_5m_input_tokens", 0)
    input_t = usage.get("input_tokens", 0)
    output_t = usage.get("output_tokens", 0)
    prompt_total = read + write_1h + write_5m + input_t
    hit = f"{read * 100 / prompt_total:.1f}%" if prompt_total else "n/a"
    return {
        "input": format_tokens(input_t),
        "output": format_tokens(output_t),
        "read": format_tokens(read),
        "write_1h": format_tokens(write_1h),
        "write_5m": format_tokens(write_5m),
        "creation": format_tokens(write_1h + write_5m),
        "hit": hit,
    }


def _model_cache_breakdown(entry: dict) -> dict:
    """Pull cache numbers off a single model_usage entry.

    Mirrors ``_cache_breakdown`` but consumes the camelCase shape the SDK
    uses on ``ResultMessage.model_usage`` entries — and without the 1h/5m
    creation split, which model_usage does not carry.
    """
    read = entry.get("cacheReadInputTokens", 0)
    creation = entry.get("cacheCreationInputTokens", 0)
    input_t = entry.get("inputTokens", 0)
    prompt_total = read + creation + input_t
    hit = f"{read * 100 / prompt_total:.1f}%" if prompt_total else "n/a"
    return {
        "read": format_tokens(read),
        "creation": format_tokens(creation),
        "hit": hit,
    }


def _log_cache_usage(usage: dict | None) -> None:
    """Log the per-turn prompt-cache token breakdown.

    write_1h tracks the 1-hour cache writes; a high read on later turns
    shows the cached prefix is being reused.
    """
    if not usage:
        return
    b = _cache_breakdown(usage)
    logger.info(
        "turn tokens: input=%s output=%s cache_read=%s hit=%s write_1h=%s write_5m=%s",
        b["input"], b["output"], b["read"], b["hit"], b["write_1h"], b["write_5m"],
    )


@dataclass
class TextEvent:
    text: str


@dataclass
class ToolEvent:
    name: str
    summary: str


@dataclass
class ResultEvent:
    text: str
    is_error: bool


@dataclass
class TodoEvent:
    """A TodoWrite snapshot: the full task list as (content, status, active_form)."""

    items: list[tuple[str, str, str]]


@dataclass
class TaskEvent:
    """A subagent lifecycle event.

    phase is one of:
      - started:      a subagent has begun running.
      - progress:     an interim tool-use update from a running subagent.
      - acknowledged: SDK has marked the task as completed/failed/stopped via
                      task_updated. Bookkeeping only — no phone message; lets
                      drain and the "still running" notice skip subagents the
                      SDK already considers done, even if no TaskNotification-
                      Message follows.
      - notification: a subagent finished (status carries completed/failed/stopped).
                      duration_ms and total_tokens carry the SDK-reported usage.
      - timeout:      a detached background subagent never reported back.
    """

    phase: str
    task_id: str = ""
    description: str = ""
    status: str = ""
    summary: str = ""
    last_tool: str = ""
    duration_ms: int = 0
    total_tokens: int = 0


Event = TextEvent | ToolEvent | ResultEvent | TodoEvent | TaskEvent
PermissionHandler = Callable[[str, dict, str], Awaitable[bool]]
QuestionHandler = Callable[[dict, str], Awaitable[str]]
Emit = Callable[[Event], Awaitable[None]]


def tool_summary(name: str, tool_input: dict) -> str:
    """Short human-readable description of a tool call for phone + log display.

    Each tool's identifying field is hand-picked: Bash → the command, Skill
    → the skill id, Agent/Task → the description (what the subagent is going
    to do), ToolSearch → the query string, Task* (Get/Output/Stop/Update) →
    the task_id (full 17 chars — same width as our short ids in TaskStarted).
    File tools fall through to the generic file_path/path/pattern/url match.

    Output is run through :func:`collapse_inline_paths` so every caller (log line,
    phone ToolEvent, permission prompt) emits the same project-relative /
    ``~/…`` form. Tests without a log_context cwd get raw paths back — the
    default cwd is ``""`` which is a no-op.
    """
    return collapse_inline_paths(_tool_summary_raw(name, tool_input))


def _tool_summary_raw(name: str, tool_input: dict) -> str:
    if name == "Bash":
        return tool_input.get("command", "")
    if name == "Skill":
        return tool_input.get("skill", "")
    if name in ("Agent", "Task"):
        return tool_input.get("description") or tool_input.get("subagent_type", "")
    if name == "ToolSearch":
        return tool_input.get("query", "")
    if name == "TaskCreate":
        # Real SDK shape: {"subject": "...", "description": "..."}. `subject`
        # is the short title (e.g. "#8 geo cache TTL 60→30") — that's what
        # the operator actually wants to see, not an opaque id.
        return tool_input.get("subject") or tool_input.get("description", "")
    if name == "TaskUpdate":
        # Real SDK shape: {"taskId": "1", "status": "in_progress"} —
        # `taskId → status` makes the state transition readable at a glance.
        tid = tool_input.get("taskId") or tool_input.get("task_id", "")
        status = tool_input.get("status", "")
        if tid and status:
            return f"{tid} → {status}"
        return tid or status
    if name in ("TaskGet", "TaskOutput", "TaskStop"):
        # Inconsistent across these three: TaskGet uses taskId (camelCase),
        # TaskOutput uses task_id (snake_case). Accept both.
        return tool_input.get("taskId") or tool_input.get("task_id", "")
    if name == "AskUserQuestion":
        qs = tool_input.get("questions") or []
        if not qs:
            return ""
        first = question_preview(qs[0])
        return f"{first} (×{len(qs)})" if len(qs) > 1 else first
    if name == "Grep":
        # `pattern in path` reads naturally and surfaces both fields without
        # labels. The generic fallback would let `path` shadow `pattern` —
        # backwards for debugging, since what's being searched for is usually
        # more diagnostic than where.
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path")
        if pattern and path:
            return f"{pattern} in {path}"
        return pattern or (path or "")
    target = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("pattern")
        or tool_input.get("url")
    )
    return str(target) if target else ""


def _todo_event(tool_input: dict) -> TodoEvent:
    """Build a TodoEvent from a TodoWrite tool call's input."""
    items = [
        (
            todo.get("content", ""),
            todo.get("status", "pending"),
            todo.get("activeForm", ""),
        )
        for todo in tool_input.get("todos") or []
    ]
    return TodoEvent(items)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _format_elapsed(seconds: float) -> str:
    """Compact wall-time renderer: '420ms' under 1s, else '4.2s'. Avoids
    `0.420s` / `1.000s` style which is harder to skim than ms / s split."""
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    return f"{seconds:.1f}s"


# Tools whose input is essentially a path — middle-elide their previews so the
# filename (most identifying part) survives instead of being chopped off the
# tail when the project directory eats most of the 80-char budget.
_PATH_TOOLS = frozenset({"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"})


def _middle_elide(text: str, limit: int) -> str:
    """Truncate by removing the middle, keeping head and tail intact.

    `_truncate` chops from the right — fine for prose, terrible for paths
    where the filename (the most identifying chunk) lives at the end. For
    a 106-char path with limit=80, the right-chop kills the filename; this
    helper keeps both ends and elides the middle.
    """
    if len(text) <= limit:
        return text
    keep = limit - 1  # one char for the "…"
    head = keep // 2 + keep % 2
    tail = keep - head
    return f"{text[:head]}…{text[-tail:]}"


def _ask_question_status(content) -> str:
    """Resolve the real outcome of an AskUserQuestion ToolResultBlock.

    The SDK has no "answered with a value" callback shape — orchestrator.
    answer_question() returns the user's reply via PermissionResultDeny,
    so `is_error` is always True. The actual disposition is encoded in the
    message text written by answer_question; match its known prefixes to
    decide between ``answered`` and ``no_answer``.
    """
    raw = content
    if isinstance(raw, list):
        raw = " ".join(
            part.get("text", "") for part in raw if isinstance(part, dict)
        )
    flat = str(raw or "")
    if flat.startswith("The user answered"):
        return "answered"
    return "no_answer"


def _tool_result_preview(content, *, collapse: bool, limit: int = 80) -> tuple[str, int]:
    """Flatten a ToolResultBlock.content value into a single preview + total len.

    The SDK delivers content either as a plain string or as a list of
    ``{"type": "text", "text": "…"}`` parts (newer wire format); we accept
    both. ``collapse`` toggles project-path / $HOME collapsing — wanted for
    error stacks (paths inside the project are nicer relative) but not for
    Skill banners (the banner has no paths and we want it byte-identical to
    what the SDK announced). Returns (preview, full_len) so callers can
    signal truncation.
    """
    if not content:
        return "", 0
    raw = content
    if isinstance(raw, list):
        raw = " ".join(
            part.get("text", "") for part in raw if isinstance(part, dict)
        )
    flat = str(raw).replace("\n", " ")
    if collapse:
        flat = collapse_inline_paths(flat)
    return _truncate(flat, limit), len(flat)


def _fmt_fields(pairs: dict) -> str:
    """Render ordered key=value pairs, skipping None / "" / 0 / False values."""
    parts = [f"{k}={v}" for k, v in pairs.items() if v not in (None, "", 0, False)]
    return " ".join(parts) if parts else "-"


def _subagent_fields(parent_tool_use_id: str | None) -> dict:
    """Return the agent/sub_id/sub_type fields to lead a child message line.

    ``parent_tool_use_id`` on AssistantMessage/UserMessage being set is itself
    proof the message came from inside a subagent (the Task tool that spawned
    it). ``agent=sub`` is therefore emitted purely from that signal — works
    even when log_context's subagent map missed the matching task_started
    (daemon restart mid-subagent, etc.). ``sub_id`` + ``sub_type`` come from
    the map and pair the line to the task_started entry visually.
    Returns ``{}`` for main-turn messages so the line stays unchanged.
    """
    if not parent_tool_use_id:
        return {}
    sub_id, sub_type = log_context.lookup_subagent(parent_tool_use_id)
    return {"agent": "sub", "sub_id": sub_id, "sub_type": sub_type}


def _format_denials(denials: list) -> str | None:
    """Compact list of denied tool names, e.g. ``[Bash,Skill]``.

    SDK types ``permission_denials`` as ``list[Any]``; no schema is promised
    so each entry is probed defensively (dict with tool_name → name, else
    str()). Empty / falsy → None so the field stays out of the log.
    """
    if not denials:
        return None
    names = []
    for d in denials:
        if isinstance(d, dict):
            names.append(d.get("tool_name") or d.get("tool") or str(d))
        else:
            names.append(str(d))
    return f"[{','.join(names)}]"


def _format_unix_ts(ts: int | None) -> str | None:
    """Render a unix-seconds timestamp as ``<ts> (YYYY-MM-DD HH:MM:SS)`` local time."""
    if ts is None:
        return None
    try:
        local = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return str(ts)
    return f"{ts} ({local})"


def _sdk_message_summary(message) -> str | None:
    """Per-type one-line digest of an SDK message's key identifiers.

    Content (assistant text, tool inputs, tool results) is intentionally
    omitted — those live in the per-session JSONL transcript at
    ``~/.claude/projects/<key>/<session-id>.jsonl`` and would otherwise
    dominate the daemon log. Tool-use ids are surfaced (truncated to 8 chars)
    so a request and its result can be paired by eye.

    Returns None when the message should not be logged at INFO at all
    (StreamEvent — per-token noise; the full repr is still kept at DEBUG).
    """
    # SystemMessage subclasses must come before the SystemMessage branch.
    if isinstance(message, TaskStartedMessage):
        subagent_type = (message.data or {}).get("subagent_type")
        # Record the subagent so subsequent AssistantMessage/UserMessage
        # lines (whose parent_tool_use_id matches this task's Task tool id)
        # can render sub_id + sub_type — saves the operator from manually
        # pairing parent_tool_use_id with the task_started line above.
        log_context.record_subagent(message.tool_use_id, message.task_id, subagent_type)
        return _fmt_fields(
            {
                "task_id": message.task_id,
                "subagent_type": subagent_type,
                "desc": _truncate(message.description, 60) if message.description else None,
            }
        )
    if isinstance(message, TaskProgressMessage):
        usage = message.usage or {}
        return _fmt_fields(
            {
                "task_id": message.task_id,
                "tool_uses": usage.get("tool_uses"),
                "total_tokens": usage.get("total_tokens"),
                "last_tool": message.last_tool_name,
            }
        )
    if isinstance(message, TaskNotificationMessage):
        usage = message.usage or {}
        # Symmetric with TaskStartedMessage above; safe to call when this
        # tool_use_id was never recorded (e.g. background Bash).
        log_context.forget_subagent(message.tool_use_id)
        return _fmt_fields(
            {
                "task_id": message.task_id,
                "status": message.status,
                "duration_ms": usage.get("duration_ms"),
                "tool_uses": usage.get("tool_uses"),
                "total_tokens": usage.get("total_tokens"),
            }
        )
    if isinstance(message, MirrorErrorMessage):
        return _fmt_fields({"error": _truncate(message.error, 80) if message.error else None})
    if isinstance(message, HookEventMessage):
        data = message.data or {}
        # On hook_response, exit_code/outcome/stderr are the diagnostic gold —
        # a hook that silently exits 1 is otherwise invisible. stdout is often
        # huge (the additionalContext payload) so we skip it.
        stderr = data.get("stderr") or ""
        stderr_preview = _truncate(stderr.replace("\n", " "), 80) if stderr.strip() else None
        return _fmt_fields(
            {
                "subtype": message.subtype,
                "hook": message.hook_event_name,
                "tool": data.get("tool_name"),
                "exit_code": data.get("exit_code"),
                "outcome": data.get("outcome"),
                "stderr": stderr_preview,
            }
        )
    if isinstance(message, SystemMessage) and message.subtype == "task_updated":
        # SDK's bookkeeping signal when a task transitions state — without a
        # specific branch this fell through the generic SystemMessage path
        # and rendered as a bare empty line. The relevant fields live in
        # data.patch (status, end_time, …); surface task_id + status so the
        # operator can correlate with the preceding task_started.
        data = message.data or {}
        patch = data.get("patch") or {}
        return _fmt_fields(
            {
                "subtype": "task_updated",
                "task_id": data.get("task_id"),
                "status": patch.get("status"),
            }
        )
    if isinstance(message, SystemMessage) and message.subtype == "compact_boundary":
        # compact_boundary marks an automatic context compaction. pre_tokens
        # tells you how much was condensed, trigger says why (manual/auto/…).
        meta = (message.data or {}).get("compact_metadata") or {}
        return _fmt_fields(
            {
                "subtype": "compact_boundary",
                "pre_tokens": meta.get("pre_tokens"),
                "trigger": meta.get("trigger"),
            }
        )
    if isinstance(message, SystemMessage) and message.subtype == "permission_denied":
        # We don't have a confirmed shape for the data dict yet (couldn't
        # reproduce in test). Surface every key so the next production hit
        # tells us what's actually there — beats guessing wrong field names.
        data = message.data or {}
        fields: dict[str, str | None] = {"subtype": "permission_denied"}
        for k, v in data.items():
            if k in ("subtype", "type", "uuid", "session_id"):
                continue
            if v is None or v == "":
                continue
            fields[k] = _truncate(str(v).replace("\n", " "), 80)
        return _fmt_fields(fields)
    if isinstance(message, SystemMessage):
        data = message.data or {}
        # Don't surface session_id here: _consume runs _note_system_message
        # (which writes the new session into log_context) BEFORE _log_sdk_message,
        # so by the time we render this init the leading `session=…` column
        # already shows init.data.session_id. A separate `session_id=…` field
        # would always duplicate that column verbatim. If a future SDK ever
        # emits an init with a different session_id, the *column itself* jumps
        # — that visual change is the signal, not an extra field.
        cwd = data.get("cwd")
        if cwd:
            cwd = display_path(cwd)
        return _fmt_fields(
            {
                "subtype": message.subtype,
                "model": data.get("model"),
                "cwd": cwd,
                # Plan/auto/edit modes change tool-call behavior significantly;
                # version pins which CLI build emitted this message.
                "permission_mode": data.get("permissionMode"),
                "version": data.get("claude_code_version"),
                # _build_options forces ENABLE_PROMPT_CACHING_1H unconditionally
                # — surface it here so cache-related diagnostics start with the
                # active policy in the same line as model/cwd.
                "cache_ttl_policy": "1h",
            }
        )
    if isinstance(message, AssistantMessage):
        # Tool ids are always `toolu_<random>` — `_short_tool_id` strips that
        # prefix and keeps 8 chars so parallel calls stay distinguishable.
        # Surface the tool's input via tool_summary(): a Bash log line without
        # the command (or a Read line without the path) is barely useful for
        # debugging — `Bash#abc12345(git status)` is.
        tool_entries: list[str] = []
        text_blocks = thinking_blocks = text_len = thinking_len = 0
        text_parts: list[str] = []
        for b in message.content:
            if isinstance(b, ToolUseBlock):
                short_id = _short_tool_id(b.id)
                # Stash (id → name + start time) so the matching tool_result
                # line can render tool name and elapsed time without having to
                # cross-reference earlier log lines by eye.
                log_context.record_tool_use(b.id, b.name)
                preview = tool_summary(b.name, b.input)
                if preview:
                    preview = preview.replace("\n", " ")
                    # Path-tools middle-elide so the filename survives even
                    # when the project directory eats the truncation budget;
                    # everything else right-truncates as before.
                    if b.name in _PATH_TOOLS:
                        preview = _middle_elide(preview, 80)
                    else:
                        preview = _truncate(preview, 80)
                    tool_entries.append(f"{b.name}#{short_id}({preview})")
                else:
                    tool_entries.append(f"{b.name}#{short_id}")
            elif isinstance(b, TextBlock):
                text_blocks += 1
                text_len += len(b.text)
                text_parts.append(b.text)
            elif isinstance(b, ThinkingBlock):
                thinking_blocks += 1
                thinking_len += len(b.thinking)
        text_preview = None
        if text_parts:
            # Short intermediate replies (e.g. "Agent X done, starting Y")
            # are not echoed in ResultMessage.result (that's only the final
            # turn output), so without this preview they vanish from the log.
            joined = " ".join(text_parts).replace("\n", " ").strip()
            text_preview = f'"{_truncate(joined, 80)}"'
        # Pure-text replies (no tools, no thinking, no error/stop_reason/
        # parent_tool_use_id) are content-only — drop the metadata scaffolding
        # and just show the text. Otherwise the log is dominated by
        # `text_blocks=1 text_len=… text_preview="…" model=…` noise around
        # short chat lines.
        is_pure_text = (
            text_parts
            and not tool_entries
            and not thinking_blocks
            and not message.error
            and not message.stop_reason
            and not message.parent_tool_use_id
        )
        if is_pure_text:
            return text_preview or ""
        # Thinking blocks: surface this response's prompt/output/cache tokens
        # so the cost of an extended thinking run is visible at INFO without
        # waiting for the terminal ResultMessage. `hit` lets ops confirm the
        # cached prefix is still being reused mid-thinking.
        usage_fields: dict = {}
        if thinking_blocks and message.usage:
            b = _cache_breakdown(message.usage)
            usage_fields["input"] = b["input"]
            usage_fields["output"] = b["output"]
            usage_fields["cache_read"] = b["read"]
            usage_fields["hit"] = b["hit"]
        return _fmt_fields(
            {
                **_subagent_fields(message.parent_tool_use_id),
                "tools": f"[{','.join(tool_entries)}]" if tool_entries else None,
                # Character lengths carry more than the matching block count
                # (>0 == 1 bit). text_len > 0 implies a block was present.
                "text_len": text_len if text_blocks else None,
                "text_preview": text_preview,
                "thinking_len": thinking_len if thinking_blocks else None,
                **usage_fields,
                # `model` differs from the init model when a subagent runs
                # under a different model — the only signal of that.
                "model": message.model,
                # parent_tool_use_id != None means this message is from inside
                # a subagent invocation; otherwise it's the main turn.
                "parent_tool_use_id": _short_tool_id(message.parent_tool_use_id),
                # AssistantMessage.stop_reason is verified None on every SDK
                # stream message (CLI's wire format doesn't include it; the
                # JSONL transcript fills it from elsewhere). Surfaced only when
                # set — currently fires for the rare `stop_sequence` case.
                # The closing ResultMessage carries the real `end_turn`.
                "stop_reason": message.stop_reason,
                "error": message.error,
            }
        )
    if isinstance(message, UserMessage):
        # UserMessage content typically carries ToolResultBlocks for tool
        # output, but can also carry TextBlocks (Skill output, system
        # reminder injections). Surface both — a UserMessage with only
        # TextBlock content previously rendered as bare "-", a useless line.
        results: list[str] = []
        text_total_len = 0
        text_parts: list[str] = []
        content = message.content
        if isinstance(content, list):
            for b in content:
                if isinstance(b, ToolResultBlock):
                    short_id = _short_tool_id(b.tool_use_id)
                    # Recover the tool name and wall-clock duration the
                    # AssistantMessage stashed when this call started. None
                    # means we never saw the matching tool_use (subagent
                    # boundary, log_context cleared mid-call, …) — degrade
                    # gracefully to id-only.
                    tool_name, elapsed = log_context.take_tool_use(b.tool_use_id)
                    prefix = f"{tool_name}#{short_id}" if tool_name else short_id
                    dur_str = _format_elapsed(elapsed) if elapsed is not None else None
                    flag = "err" if b.is_error else "done"
                    if tool_name == "AskUserQuestion":
                        # SDK convention: AskUserQuestion is delivered as
                        # PermissionResultDeny so `is_error` is always True —
                        # the actual outcome (user answered vs. cancelled /
                        # timed out) lives in the message text. Resolve it
                        # here so downstream log readers don't have to.
                        flag = _ask_question_status(b.content)
                    flag_with_dur = f"{flag} {dur_str}" if dur_str else flag
                    # Two distinct reasons to surface tool_result content:
                    #   - errors: `(err)` on its own forces a JSONL dive to
                    #     learn anything; the message text is the diagnostic.
                    #   - Skill: its result is always a short banner like
                    #     ``Launching skill: superpowers:brainstorming`` —
                    #     useful confirmation that the skill actually loaded.
                    # Generic successes are skipped: file reads etc. can be
                    # huge and aren't the diagnostic target.
                    if b.is_error:
                        # Wider error window (200) than the generic 80: error
                        # stacks lose their diagnostic value when chopped too
                        # short. content_len signals when truncation happened
                        # so the operator knows to grep the JSONL transcript
                        # for the full payload.
                        preview, full_len = _tool_result_preview(
                            b.content, collapse=True, limit=200
                        )
                        suffix = f": {preview}" if preview else ""
                        if full_len > 200:
                            suffix = f"{suffix} content_len={full_len}"
                    elif tool_name == "Skill":
                        preview, _ = _tool_result_preview(b.content, collapse=False)
                        suffix = f": {preview}" if preview else ""
                    else:
                        suffix = ""
                    results.append(f"{prefix}({flag_with_dur}{suffix})")
                elif isinstance(b, TextBlock):
                    text_total_len += len(b.text)
                    text_parts.append(b.text)
        text_preview = None
        if text_parts:
            joined = " ".join(text_parts).replace("\n", " ").strip()
            text_preview = f'"{_truncate(joined, 80)}"'
        return _fmt_fields(
            {
                **_subagent_fields(message.parent_tool_use_id),
                "tool_results": f"[{','.join(results)}]" if results else None,
                "text_len": text_total_len or None,
                "text_preview": text_preview,
                "parent_tool_use_id": _short_tool_id(message.parent_tool_use_id),
            }
        )
    if isinstance(message, ResultMessage):
        denials = message.permission_denials or []
        errors = message.errors or []
        errors_str = (
            f"{len(errors)}:{_truncate(str(errors[0]), 80)}" if errors else None
        )
        cost = (
            f"${message.total_cost_usd:.4f}"
            if message.total_cost_usd is not None
            else None
        )
        # Without this preview the daemon log carries zero trace of what the
        # model actually concluded — `text_blocks=1` is a bit, not a record.
        result_preview = None
        if message.result:
            flat = message.result.replace("\n", " ")
            result_preview = f'"{_truncate(flat, 80)}"'
        usage = message.usage or {}
        # service_tier defaults to "standard"; inference_geo defaults to "" —
        # surface only when they deviate. A turn that suddenly runs on a
        # different tier or region is the kind of thing that explains a
        # latency or cost anomaly.
        service_tier = usage.get("service_tier")
        if service_tier == "standard":
            service_tier = None
        inference_geo = usage.get("inference_geo") or None
        # iterations: only fires (length>1) when Anthropic's server splits one
        # logical response into multiple internal model calls (rare; long
        # extended thinking or server-side retry). The common length==1 case
        # would duplicate the outer usage and just be noise.
        iterations = usage.get("iterations") or []
        iterations_str = f"{len(iterations)}" if len(iterations) > 1 else None
        return _fmt_fields(
            {
                "subtype": message.subtype,
                "duration_ms": message.duration_ms,
                # api vs total separates API time from SDK/queue overhead —
                # the diff explains "why did this turn feel slow".
                "duration_api_ms": message.duration_api_ms,
                "num_turns": message.num_turns,
                "stop_reason": message.stop_reason,
                "cost": cost,
                "permission_denials": _format_denials(denials),
                "errors": errors_str,
                "is_error": message.is_error or None,
                "api_error_status": message.api_error_status,
                "service_tier": service_tier,
                "inference_geo": inference_geo,
                "iterations": iterations_str,
                "result": result_preview,
            }
        )
    if isinstance(message, RateLimitEvent):
        info = message.rate_limit_info
        return _fmt_fields(
            {
                "status": info.status,
                "type": info.rate_limit_type,
                "utilization": (
                    f"{info.utilization * 100:.1f}%"
                    if info.utilization is not None
                    else None
                ),
                "resets_at": _format_unix_ts(info.resets_at),
                "overage_status": info.overage_status,
                "overage_resets_at": _format_unix_ts(info.overage_resets_at),
                "overage_disabled_reason": info.overage_disabled_reason,
            }
        )
    if isinstance(message, StreamEvent):
        # Per-token noise; full repr still goes to DEBUG.
        return None
    return "-"


# Verbs match the SDK wire-format naming (`{"type": "assistant"}`,
# `{"type": "rate_limit_event"}`, …) so what shows up in the log is the same
# token the SDK uses internally — no translation layer to learn. Known
# SystemMessage subtypes are promoted to top-level verbs (matching their
# wire-level subtype name) for the same reason.
_VERB_BY_TYPE: dict[type, str] = {
    AssistantMessage: "assistant",
    UserMessage: "user",
    ResultMessage: "result",
    RateLimitEvent: "rate_limit_event",
    TaskStartedMessage: "task_started",
    TaskProgressMessage: "task_progress",
    TaskNotificationMessage: "task_notification",
    MirrorErrorMessage: "mirror_error",
}
_SYSTEM_SUBTYPE_VERBS: set[str] = {
    "init", "permission_denied", "compact_boundary", "task_updated",
}


def _message_verb(message) -> str:
    """Pick the leading word for the log line, matching SDK wire naming.

    Order matters: Hook/Task/MirrorError messages are SystemMessage
    *subclasses*, so HookEventMessage and the exact-type lookup must come
    before the generic SystemMessage branch — otherwise a TaskStartedMessage
    would render with verb "system" instead of "task_started".
    """
    if isinstance(message, HookEventMessage):
        # SDK wire subtype is "hook_started" / "hook_response" — use directly.
        return message.subtype
    if isinstance(message, AssistantMessage):
        # Extended-thinking turns deliver multiple AssistantMessages per turn,
        # boundaried by content_block_stop. A snapshot whose content is solely
        # ThinkingBlock(s) (no text, no tool_use) is the thinking phase
        # closing — promote it to its own verb so it doesn't masquerade as a
        # plain assistant usage row downstream.
        if message.content and all(isinstance(b, ThinkingBlock) for b in message.content):
            return "thinking"
        return "assistant"
    if type(message) in _VERB_BY_TYPE:
        return _VERB_BY_TYPE[type(message)]
    if isinstance(message, SystemMessage):
        # Promote well-known SystemMessage subtypes (init, permission_denied,
        # compact_boundary, task_updated) to top-level verbs — `init …` reads
        # cleaner than `system subtype=init …`. Strip the redundant subtype
        # field from the summary in this case (caller handles).
        if message.subtype in _SYSTEM_SUBTYPE_VERBS:
            return message.subtype
        return "system"
    return type(message).__name__.lower()


def _strip_subtype_if_redundant(message, summary: str) -> str:
    """When the verb already conveys the subtype, drop the leading
    `subtype=<x> ` prefix so the line doesn't say it twice."""
    sub: str | None = None
    if isinstance(message, HookEventMessage):
        sub = message.subtype
    elif isinstance(message, SystemMessage) and message.subtype in _SYSTEM_SUBTYPE_VERBS:
        sub = message.subtype
    if sub:
        prefix = f"subtype={sub} "
        if summary.startswith(prefix):
            return summary[len(prefix):]
        if summary == f"subtype={sub}":  # pragma: no branch
            # False arm unreachable: when sub is set, summary always starts
            # with `subtype=<sub> ` (matched above) or equals `subtype=<sub>`
            # — the renderer never emits a summary without that prefix.
            return ""
    return summary


def _is_quiet_rate_limit(message) -> bool:
    """A rate limit event in the steady normal state — same INFO line every
    turn. Demote to DEBUG to cut noise; abnormal states (warning, rejected,
    overage active) still surface at INFO.

    `rejected` here is the *overage* status, meaning overage purchases are
    disabled — that's the normal account setup, not a problem. The actual
    failure modes are `status` going to `allowed_warning` / `rejected`, or
    `overage_status` becoming `active`.
    """
    if not isinstance(message, RateLimitEvent):
        return False
    info = message.rate_limit_info
    return (
        info.status == "allowed"
        and info.overage_status in ("rejected", None)
    )


def _is_quiet_message(message) -> bool:
    """True for messages that should default to DEBUG rather than INFO.

    task_progress fires once per subagent tool call and carries the same
    last_tool value as the assistant message that immediately follows — the
    only unique info is the running token/tool counter, which is also
    aggregated into the terminal task_notification. DEBUG keeps the forensic
    detail without flooding INFO.

    Hook events (hook_started, hook_response) are pure noise at INFO — no
    fields they carry are operationally useful on the daemon log; the full
    repr still goes to DEBUG for the rare diagnostic dive.
    """
    if _is_quiet_rate_limit(message):
        return True
    if isinstance(message, TaskProgressMessage):
        return True
    if isinstance(message, HookEventMessage):
        return True
    return False


def _log_sdk_message(message) -> None:
    """Trace each SDK message in the daemon log.

    INFO carries a per-type compact summary; the full repr is kept at DEBUG
    for deep diagnostics. The complete conversation is already preserved by
    the SDK in ``~/.claude/projects/<key>/<session-id>.jsonl``, so the daemon
    log doesn't need to duplicate it.
    """
    name = type(message).__name__
    summary = _sdk_message_summary(message)
    if summary is None:
        logger.debug("sdk_message_full %s %r", name, message)
        return
    verb = _message_verb(message)
    summary = _strip_subtype_if_redundant(message, summary)
    line = f"{verb} {summary}" if summary else verb
    if _is_quiet_message(message):
        logger.debug("%s", line)
    else:
        logger.info("%s", line)
    logger.debug("sdk_message_full %s %r", name, message)


def _track_pending(event: Event, pending: set[str]) -> None:
    """Fold a TaskEvent into the set of background agents still running."""
    if isinstance(event, TaskEvent):
        if event.phase == "started":
            pending.add(event.task_id)
        elif event.phase == "notification":
            pending.discard(event.task_id)


def _translate(
    message,
    subagents: dict[str, str] | None = None,
    acknowledged: set[str] | None = None,
) -> list[Event]:
    # ``subagents`` maps a live subagent's task_id → its description. Populated
    # at task_started, drained at task_notification so the completion event can
    # carry the description (which the SDK only includes on task_started).
    # ``acknowledged`` accumulates task_ids the SDK has flagged as terminal via
    # task_updated — used to short-circuit drain and suppress "still running"
    # noise for subagents the SDK already considers done.
    if subagents is None:
        subagents = {}
    if acknowledged is None:
        acknowledged = set()
    events: list[Event] = []
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock) and block.text.strip():
                events.append(TextEvent(block.text.strip()))
            elif isinstance(block, ToolUseBlock):
                # TodoWrite carries the full task list — render it as progress
                # rather than a bare tool line.
                if block.name == "TodoWrite":
                    events.append(_todo_event(block.input))
                else:
                    events.append(
                        ToolEvent(block.name, tool_summary(block.name, block.input))
                    )
    elif isinstance(message, ResultMessage):
        events.append(ResultEvent(message.result or "", bool(message.is_error)))
    elif isinstance(message, TaskStartedMessage):
        # Every task — subagents AND plain commands (Bash, …) — arrives as
        # task_started. Only a real subagent carries subagent_type; surface
        # just those as "Subagent". Commands are already shown via ToolEvent.
        subagent_type = (message.data or {}).get("subagent_type")
        if subagent_type:
            subagents[message.task_id] = message.description
            events.append(
                TaskEvent(
                    "started", message.task_id, description=message.description
                )
            )
    elif isinstance(message, TaskProgressMessage):
        if (message.data or {}).get("subagent_type"):
            events.append(
                TaskEvent(
                    "progress",
                    message.task_id,
                    description=message.description,
                    last_tool=message.last_tool_name or "",
                )
            )
    elif isinstance(message, TaskNotificationMessage):
        # task_notification carries no subagent_type — recognise a subagent's
        # by the id recorded at task_started.
        if message.task_id in subagents:
            description = subagents.pop(message.task_id)
            usage = message.usage or {}
            events.append(
                TaskEvent(
                    "notification",
                    message.task_id,
                    description=description,
                    status=message.status,
                    summary=message.summary,
                    duration_ms=usage.get("duration_ms", 0) or 0,
                    total_tokens=usage.get("total_tokens", 0) or 0,
                )
            )
    elif isinstance(message, SystemMessage) and message.subtype == "task_updated":
        # SDK's authoritative "task hit a terminal state" signal. Fires for
        # every subagent — including ones whose answer is inlined into the
        # parent's turn so no TaskNotificationMessage follows. We record the
        # task_id and emit a bookkeeping event so the orchestrator can stop
        # warning about it; drain logic reads ``acknowledged`` directly.
        data = message.data or {}
        task_id = data.get("task_id")
        status = (data.get("patch") or {}).get("status")
        if task_id and status in ("completed", "failed", "stopped"):
            acknowledged.add(task_id)
            events.append(TaskEvent("acknowledged", task_id, status=status))
    elif isinstance(message, SystemMessage) and message.subtype == "compact_boundary":
        meta = (message.data or {}).get("compact_metadata") or {}
        pre = meta.get("pre_tokens")
        if pre:
            events.append(
                TextEvent(f"🗜 Context compacted (was {pre} tokens).")
            )
        else:
            events.append(TextEvent("🗜 Context compacted."))
    return events


class ClaudeRunner:
    """Runs Claude Code turns via the Agent SDK, one turn at a time."""

    def __init__(self):
        self.permission_handler: PermissionHandler | None = None
        self.question_handler: QuestionHandler | None = None
        self._session_ids: dict[str, str] = {}
        self._active_client: ClaudeSDKClient | None = None
        self.proxy_url: str | None = None
        # Set while _drain_background is running so a new prompt can pre-empt
        # the post-turn wait for a missing background-agent notification.
        self._drain_cancel: asyncio.Event | None = None
        # Per-project cumulative token tally and last turn's raw usage,
        # both scoped to the current session (cleared on reset/switch).
        self._session_tokens: dict[str, int] = {}
        self._last_usage: dict[str, dict] = {}
        # Same scope, but split per model (main + subagents). The SDK reports
        # this on ResultMessage.model_usage with camelCase keys distinct from
        # `usage`. Empty when a turn has no model_usage payload.
        self._session_model_tokens: dict[str, dict[str, int]] = {}
        self._last_model_usage: dict[str, dict] = {}
        # Per-project monotonic turn id, scoped to the current session so the
        # count restarts at 1 whenever the session is cleared or switched.
        # Surfaced via log_context for grep-by-session-and-turn slicing.
        self._turn_counts: dict[str, int] = {}
        # Runtime model override set via /model, and the model observed from
        # the SDK init message — both global, not persisted to config.
        self._model: str | None = None
        self._observed_model: str | None = None

    def reset(self, project_path: str) -> None:
        self._session_ids.pop(project_path, None)
        self._session_tokens.pop(project_path, None)
        self._last_usage.pop(project_path, None)
        self._session_model_tokens.pop(project_path, None)
        self._last_model_usage.pop(project_path, None)
        self._turn_counts.pop(project_path, None)

    def set_session(self, project_path: str, session_id: str) -> None:
        self._session_ids[project_path] = session_id
        # A different session means a different conversation — restart the
        # tally so /status reflects the resumed session, not the old one.
        self._session_tokens.pop(project_path, None)
        self._last_usage.pop(project_path, None)
        self._session_model_tokens.pop(project_path, None)
        self._last_model_usage.pop(project_path, None)
        self._turn_counts.pop(project_path, None)

    def next_turn(self, project_path: str) -> int:
        """Bump and return this project's turn counter for the current session."""
        nxt = self._turn_counts.get(project_path, 0) + 1
        self._turn_counts[project_path] = nxt
        return nxt

    def current_session(self, project_path: str) -> str | None:
        return self._session_ids.get(project_path)

    def session_tokens(self, project_path: str) -> int:
        """Cumulative tokens consumed by the current session for a project."""
        return self._session_tokens.get(project_path, 0)

    def last_usage(self, project_path: str) -> dict | None:
        """Raw usage dict from the project's most recent turn, if any."""
        return self._last_usage.get(project_path)

    def session_model_tokens(self, project_path: str) -> dict[str, int]:
        """Cumulative tokens for the current session, split per model.

        Empty when no turn yet carried a model_usage payload.
        """
        return dict(self._session_model_tokens.get(project_path, {}))

    def last_model_usage(self, project_path: str) -> dict | None:
        """Raw model_usage dict from the project's most recent turn, if any."""
        return self._last_model_usage.get(project_path)

    def set_model(self, model: str | None) -> None:
        """Set (or clear, if None) the model override applied to subsequent turns."""
        self._model = model

    @property
    def model_override(self) -> str | None:
        """The model override set via set_model, or None for the SDK default."""
        return self._model

    @property
    def observed_model(self) -> str | None:
        """The model reported by the SDK init message, or None until a turn has run."""
        return self._observed_model

    def _note_system_message(self, message, project_path: str) -> None:
        """Capture the model from the SDK init message for /model display, and
        refresh the log-context session id once the SDK reports it.

        A brand-new session has no id known to us until init lands; resumed
        sessions already had one set by run_turn. Either way, init carries the
        authoritative value, so update unconditionally.

        We also cache the session id into ``self._session_ids[project_path]``
        immediately on init so the can_use_tool callback (which runs in a
        separate task forked by the SDK at connect-time and therefore can't
        see the contextvar update we do here) has somewhere to read the
        current id back from. Without this cache, turn 1 of a brand-new
        project produces orchestrator log lines stamped ``session=-`` for
        ask_user_question / permission events — the contextvar in the
        callback's task is still the ``-`` that ``orchestrator._run`` set
        before connect.
        """
        if isinstance(message, SystemMessage) and message.subtype == "init":
            data = message.data or {}
            observed = data.get("model")
            if observed:
                self._observed_model = observed
            session_id = data.get("session_id")
            if session_id:
                log_context.set_session(session_id)
                self._session_ids[project_path] = session_id

    def record_usage(
        self,
        project_path: str,
        usage: dict | None,
        model_usage: dict | None = None,
    ) -> None:
        """Fold one turn's usage into the project's running token tally.

        ``model_usage`` mirrors ResultMessage.model_usage — a dict keyed by
        model name with camelCase token fields. When present it is the
        authoritative source: ``model_usage[<main_model>]`` matches ``usage``
        byte-for-byte, and subagent models appear as extra entries that
        ``usage`` does not include. So the session-wide total prefers the
        ``model_usage`` sum (correct main + subagents); only when the SDK
        omits ``model_usage`` do we fall back to the main-only ``usage`` sum.
        """
        if not usage:
            return
        self._last_usage[project_path] = usage
        if model_usage:
            self._last_model_usage[project_path] = model_usage
            bucket = self._session_model_tokens.setdefault(project_path, {})
            turn_total = 0
            for model, entry in model_usage.items():
                per_model_total = (
                    entry.get("inputTokens", 0)
                    + entry.get("outputTokens", 0)
                    + entry.get("cacheReadInputTokens", 0)
                    + entry.get("cacheCreationInputTokens", 0)
                )
                bucket[model] = bucket.get(model, 0) + per_model_total
                turn_total += per_model_total
        else:
            turn_total = (
                usage.get("input_tokens", 0)
                + usage.get("output_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
            )
        self._session_tokens[project_path] = (
            self._session_tokens.get(project_path, 0) + turn_total
        )

    async def interrupt(self) -> None:
        if self._active_client is not None:
            await self._active_client.interrupt()

    @property
    def is_draining(self) -> bool:
        """True while run_turn is sitting in _drain_background waiting for a
        missing background-agent notification — i.e. the main turn already
        produced its reply and only the post-turn safety wait is keeping the
        SDK client alive."""
        return self._drain_cancel is not None

    def cancel_drain(self) -> None:
        """Signal _drain_background to exit early (no timeout TaskEvents).

        Safe to call any time — a no-op when not in the drain phase. The
        drain loop races each SDK message read against this event, so the
        next iteration (or the in-flight read) unblocks shortly after.
        """
        if self._drain_cancel is not None:
            self._drain_cancel.set()

    def _build_options(self, project_path: str) -> ClaudeAgentOptions:
        async def _can_use_tool(tool_name, input_data, context):
            # SDK forks this callback's task at connect() — before init lands
            # — so the inherited log_context.session is stale on turn 1.
            # Restamp from our cache so handler log lines carry the right id.
            cached = self._session_ids.get(project_path)
            if cached:
                log_context.set_session(cached)
            if tool_name == "AskUserQuestion" and self.question_handler is not None:
                answer = await self.question_handler(input_data, project_path)
                return PermissionResultDeny(message=answer, interrupt=False)
            assert self.permission_handler is not None
            approved = await self.permission_handler(tool_name, input_data, project_path)
            if approved:
                return PermissionResultAllow()
            return PermissionResultDeny(
                message="Denied by user via DingTalk", interrupt=False
            )

        kwargs: dict = {"cwd": project_path, "can_use_tool": _can_use_tool}
        # Keep the system prompt prefix byte-stable so prompt caching hits:
        # the preset's dynamic sections (git status, …) are stripped and
        # re-injected into the first user message instead.
        kwargs["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "exclude_dynamic_sections": True,
        }
        session_id = self._session_ids.get(project_path)
        if session_id:
            kwargs["resume"] = session_id
        if self._model:
            kwargs["model"] = self._model
        # Force the 1-hour prompt cache TTL — phone turns are minutes apart,
        # so the default 5-minute window is almost always cold.
        # Override the SDK's default "sdk-py" entrypoint so daemon-produced
        # sessions show up in the desktop TUI's /resume picker (which hides
        # sdk-py / sdk-cli sessions).
        env = {
            "ENABLE_PROMPT_CACHING_1H": "1",
            "CLAUDE_CODE_ENTRYPOINT": "claude-dingtalk-bridge",
        }
        if self.proxy_url:
            env.update(
                {
                    "http_proxy": self.proxy_url,
                    "https_proxy": self.proxy_url,
                    "HTTP_PROXY": self.proxy_url,
                    "HTTPS_PROXY": self.proxy_url,
                }
            )
        kwargs["env"] = env
        return ClaudeAgentOptions(**kwargs)

    async def _consume(
        self,
        message,
        project_path: str,
        emit: Emit,
        pending: set[str],
        subagents: dict[str, str],
        acknowledged: set[str],
    ) -> None:
        """Translate one SDK message, emit its events, fold in turn bookkeeping."""
        # _note_system_message runs FIRST so the init message's own log line
        # already carries the newly-known session_id — otherwise the `init …`
        # row would render with `session=-` and only subsequent rows show
        # the real id.
        self._note_system_message(message, project_path)
        _log_sdk_message(message)
        for event in _translate(message, subagents, acknowledged):
            _track_pending(event, pending)
            await emit(event)
        if isinstance(message, ResultMessage):
            _log_cache_usage(message.usage)
            self.record_usage(project_path, message.usage, message.model_usage)
            if message.session_id:
                self._session_ids[project_path] = message.session_id
                # The SDK occasionally rotates session_id mid-conversation;
                # refresh the log context so the post-Result lines (cache
                # usage, drain) stay tagged with the current value.
                log_context.set_session(message.session_id)

    async def run_turn(self, project_path: str, prompt: str, emit: Emit) -> None:
        options = self._build_options(project_path)
        client = ClaudeSDKClient(options=options)
        # Publish the client *before* the connect await: a /stop landing while
        # connect() is suspended would otherwise see _active_client=None and
        # skip the interrupt, leaving connect to be torn down by raw cancel.
        self._active_client = client
        pending: set[str] = set()
        subagents: dict[str, str] = {}
        acknowledged: set[str] = set()
        try:
            await client.connect()
            await client.query(prompt)
            # One generator spans both phases — the second loop resumes it
            # where the ResultMessage break left off.
            messages = client.receive_messages()
            async for message in messages:
                await self._consume(
                    message, project_path, emit, pending, subagents, acknowledged
                )
                if isinstance(message, ResultMessage):
                    break
            if pending:
                await self._drain_background(
                    messages, project_path, emit, pending, subagents, acknowledged
                )
        finally:
            self._active_client = None
            # Shield disconnect from an outer cancel — a second cancel arriving
            # mid-teardown would otherwise leave the SDK subprocess as an orphan.
            # Cap the wait so a hung disconnect can't pin the task forever.
            try:
                await asyncio.wait_for(
                    asyncio.shield(client.disconnect()), timeout=5.0
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "SDK client disconnect did not complete cleanly", exc_info=True
                )

    async def _drain_background(
        self,
        messages,
        project_path: str,
        emit: Emit,
        pending: set[str],
        subagents: dict[str, str],
        acknowledged: set[str],
    ) -> None:
        """Keep the session alive past the turn so a detached background
        agent's completion notification still has a live receiver — the
        per-turn disconnect would otherwise drop it for good. Each message
        read is raced against `_drain_cancel` so a fresh prompt can pre-empt
        the wait rather than queueing behind a stale turn.

        Two timeouts gate the wait:
          - ``_SETTLE_TIMEOUT`` (per-iteration): kicks in once every pending
            task is acknowledged. Quiet stream → silent exit; any incoming
            message resets it, so a late relay turn isn't cut off.
          - ``_STUCK_TIMEOUT`` (outer): hard cap for the truly-stuck case
            (subagent that never reaches a terminal task_updated). On expiry,
            we emit a `timeout` event only for un-acknowledged task_ids.
        """
        self._drain_cancel = asyncio.Event()
        iterator = messages.__aiter__()
        try:
            async with asyncio.timeout(_STUCK_TIMEOUT):
                while True:
                    settle = (
                        _SETTLE_TIMEOUT if pending <= acknowledged else None
                    )
                    next_msg = asyncio.ensure_future(iterator.__anext__())
                    cancel_wait = asyncio.ensure_future(self._drain_cancel.wait())
                    done, _ = await asyncio.wait(
                        [next_msg, cancel_wait],
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=settle,
                    )
                    if not done:
                        # Settle window expired with no SDK activity — every
                        # remaining pending is acknowledged, the stream has
                        # settled, no late relay turn is coming. Exit silently.
                        next_msg.cancel()
                        cancel_wait.cancel()
                        await asyncio.gather(
                            next_msg, cancel_wait, return_exceptions=True
                        )
                        return
                    if next_msg not in done:
                        # Pre-empted by cancel_drain — abandon the read and
                        # return silently. No timeout event: the user is
                        # explicitly moving on.
                        next_msg.cancel()
                        await asyncio.gather(
                            next_msg, cancel_wait, return_exceptions=True
                        )
                        return
                    cancel_wait.cancel()
                    try:
                        message = next_msg.result()
                    except StopAsyncIteration:
                        return
                    await self._consume(
                        message, project_path, emit, pending, subagents,
                        acknowledged,
                    )
                    # A background completion is followed by a re-invocation
                    # turn that relays the agent's answer. Stop only once that
                    # turn lands (ResultMessage with nothing left pending), or
                    # the answer text would be cut off.
                    if not pending and isinstance(message, ResultMessage):
                        return
                    if self._drain_cancel.is_set():
                        return
        except asyncio.TimeoutError:
            # Hard cap reached. Only tasks the SDK never marked terminal are
            # "truly stuck" and worth surfacing — acknowledged ones are silent
            # since the SDK already told us they finished, we just never got
            # the relay turn.
            stuck = pending - acknowledged
            if stuck:
                logger.warning(
                    "Background agent wait timed out; %d task(s) never reported",
                    len(stuck),
                )
                for task_id in list(stuck):
                    await emit(TaskEvent("timeout", task_id))
        finally:
            self._drain_cancel = None
