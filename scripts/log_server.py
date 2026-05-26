"""Live-tailing HTTP viewer for the daemon log files.

Usage:
    python scripts/log_server.py [--port 8765] [--out PATH] [--err PATH]
                                 [--since "YYYY-MM-DD[ HH:MM[:SS]]"]
                                 [--until "YYYY-MM-DD[ HH:MM[:SS]]"]
                                 [--tail-bytes 262144]

Open http://localhost:8765/ in a browser. stdout/stderr live in separate tabs;
both keep tailing whether you're on the tab or not.

Date-range filtering:
* --since binary-searches the file for the first entry >= since, so a 100 MB
  log doesn't get scanned linearly.
* --until filters incoming entries; if it's fully in the past the watcher
  stops polling.
* Both bounds are inclusive. Bare dates expand: since=00:00:00, until=23:59:59.
"""

from __future__ import annotations

import argparse
import ast
import collections
import datetime
import errno
import functools
import hashlib
import html
import json
import queue
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, Iterator
from urllib.parse import parse_qs, urlparse

# Pull default log paths from the project so this stays in sync with launchd.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
try:
    from claude_dingtalk_bridge.launchd import LOG_DIR as _LOG_DIR  # noqa: E402
    from claude_dingtalk_bridge.display import format_tokens  # noqa: E402

    DEFAULT_OUT = _LOG_DIR / "daemon.out.log"
    DEFAULT_ERR = _LOG_DIR / "daemon.err.log"
except Exception:  # pragma: no cover — import-time fallback, requires the
    # daemon package to be missing entirely; not reachable in CI / tests.
    _LOG_DIR = Path.home() / "Library/Logs/claude-dingtalk-bridge"
    DEFAULT_OUT = _LOG_DIR / "daemon.out.log"
    DEFAULT_ERR = _LOG_DIR / "daemon.err.log"

    def format_tokens(n: int) -> str:
        return str(n)

# Favicon — read once at startup; package resource. None if unavailable.
ICON_BYTES: bytes | None = None
try:
    import claude_dingtalk_bridge as _pkg  # noqa: E402

    _icon = Path(_pkg.__file__).parent / "resources" / "icon.png"
    if _icon.exists():
        ICON_BYTES = _icon.read_bytes()
except Exception:  # pragma: no cover — icon load failure, defensive only.
    pass

# --- Log line parsing / HTML rendering -------------------------------------
#
# Daemon logs mix plain INFO lines with Python `repr()` of SDK message objects
# (AssistantMessage, ToolUseBlock, hook payloads with embedded JSON strings,
# ...). The renderer parses each repr via ast and produces a collapsible tree
# with embedded JSON auto-pretty-printed.

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"(?P<level>[A-Z]+) "
    r"(?P<module>\S+) "
    r"(?P<rest>.*)$",
    re.DOTALL,
)

# SDK repr renderer (reserved): the daemon now emits structured key=value
# logs, but verbose/future SDK output may restore `sdk_message <kind> <repr>`
# lines — keep this AST/repr parser as the first dispatch branch.
SDK_MSG_RE = re.compile(r"^sdk_message (?P<kind>\w+) (?P<payload>.+)$", re.DOTALL)

# Keys whose string values are likely embedded JSON we should auto-pretty-print.
JSON_LIKELY_KEYS = {"output", "stdout", "input", "patch", "raw"}

# Type → CSS class for header tint.
TYPE_CLASS = {
    "AssistantMessage": "t-assistant",
    "UserMessage": "t-user",
    "SystemMessage": "t-system",
    "HookEventMessage": "t-hook",
    "ResultMessage": "t-result",
    "TaskStartedMessage": "t-task",
    "TaskNotificationMessage": "t-task",
    "RateLimitEvent": "t-rate",
    "ThinkingBlock": "t-thinking",
    "TextBlock": "t-text",
    "ToolUseBlock": "t-tooluse",
    "ToolResultBlock": "t-toolresult",
}


def ast_to_obj(node):
    """Convert a Python repr AST into JSON-serializable data.

    Class calls like AssistantMessage(content=..., usage=...) become
    {"__type__": "AssistantMessage", "content": ..., "usage": ...}.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [ast_to_obj(e) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return [ast_to_obj(e) for e in node.elts]
    if isinstance(node, ast.Set):
        return [ast_to_obj(e) for e in node.elts]
    if isinstance(node, ast.Dict):
        out = {}
        for k, v in zip(node.keys, node.values):
            key = ast_to_obj(k) if k is not None else "<None>"
            out[str(key)] = ast_to_obj(v)
        return out
    if isinstance(node, ast.Call):
        func = node.func
        type_name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else "<call>"
        )
        result = {"__type__": type_name}
        for i, arg in enumerate(node.args):
            result[f"_arg{i}"] = ast_to_obj(arg)
        for kw in node.keywords:
            result[kw.arg or "**"] = ast_to_obj(kw.value)
        return result
    if isinstance(node, ast.Name):
        if node.id in ("True", "False", "None"):  # pragma: no cover
            # Pre-3.8 emitted True/False/None as Name nodes; modern Python
            # gives Constant, so this branch is legacy defensive code only.
            return {"True": True, "False": False, "None": None}[node.id]
        return f"<{node.id}>"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        v = ast_to_obj(node.operand)
        if isinstance(v, (int, float)):
            return -v
        return f"-{v}"
    if isinstance(node, ast.Expression):
        return ast_to_obj(node.body)
    return f"<unparsed:{type(node).__name__}>"


def parse_repr(text: str):
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return None
    return ast_to_obj(tree)


def try_json_parse(s: str):
    s = s.strip()
    if not s or s[0] not in "{[\"":
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None


def render_string(s: str, key_hint: str | None = None) -> str:
    """Render a string. If it looks like embedded JSON, pretty-print it."""
    if len(s) > 60 and (key_hint in JSON_LIKELY_KEYS or s.lstrip().startswith(("{", "["))):
        parsed = try_json_parse(s)
        if parsed is not None:
            inner = render_value(parsed)
            return (
                f'<details class="json-embed" open><summary>'
                f'<span class="badge">JSON, {len(s)} chars</span></summary>'
                f'{inner}</details>'
            )
    if "\n" in s or len(s) > 200:
        return f'<pre class="str">{html.escape(s)}</pre>'
    return f'<span class="str-inline">{html.escape(s)}</span>'


def render_value(v, key_hint: str | None = None) -> str:
    if v is None:
        return '<span class="lit null">None</span>'
    if isinstance(v, bool):
        return f'<span class="lit bool">{v}</span>'
    if isinstance(v, (int, float)):
        return f'<span class="lit num">{v}</span>'
    if isinstance(v, str):
        return render_string(v, key_hint)
    if isinstance(v, list):
        if not v:
            return '<span class="empty">[ ]</span>'
        items = "".join(f'<li>{render_value(x)}</li>' for x in v)
        return f'<ol class="list">{items}</ol>'
    if isinstance(v, dict):
        return render_dict(v)
    return f'<span class="str-inline">{html.escape(str(v))}</span>'


def render_dict(d: dict) -> str:
    type_name = d.get("__type__")
    if type_name:
        cls = TYPE_CLASS.get(type_name, "t-default")
        kvs = [(k, v) for k, v in d.items() if k != "__type__"]
        summary = ""
        for k, v in kvs:
            if isinstance(v, str) and 0 < len(v) < 80 and "\n" not in v:
                summary = f' <span class="summary">{html.escape(k)}={html.escape(v)}</span>'
                break
        rows = "".join(
            f'<tr><th>{html.escape(k)}</th><td>{render_value(v, k)}</td></tr>'
            for k, v in kvs
        )
        return (
            f'<details class="obj {cls}" open>'
            f'<summary><span class="type">{html.escape(type_name)}</span>{summary}'
            f' <span class="count">({len(kvs)} fields)</span></summary>'
            f'<table>{rows}</table></details>'
        )
    if not d:
        return '<span class="empty">{ }</span>'
    rows = "".join(
        f'<tr><th>{html.escape(str(k))}</th><td>{render_value(v, str(k))}</td></tr>'
        for k, v in d.items()
    )
    return f'<table class="plain">{rows}</table>'


def _render_sdk_message(
    idx: int,
    ts: str,
    level: str,
    module: str,
    kind: str,
    payload_text: str,
    raw_line: str = "",
) -> str:
    """Render an `sdk_message <kind> <repr>` log line as a typed-tree card."""
    cls = TYPE_CLASS.get(kind, "t-default")
    obj = parse_repr(payload_text)
    if obj is None:
        body = f'<pre class="raw">{html.escape(payload_text)}</pre>'
    else:
        body = render_value(obj)
    raw_attr = (
        f' data-raw="{html.escape(raw_line, quote=True)}"' if raw_line else ""
    )
    return (
        f'<article class="entry sdk {cls}" id="L{idx}"{raw_attr}>'
        f'<header><span class="ln">#{idx}</span>'
        f'<span class="ts">{ts}</span>'
        f'<span class="lvl">{level}</span>'
        f'<span class="mod">{module}</span>'
        f'<span class="kind">{kind}</span></header>'
        f'<div class="body">{body}</div></article>'
    )


# ─── end SDK repr renderer ────────────────────────────────────────────────


# ─── Structured (key=value) renderer ──────────────────────────────────────
# Main path: parse the daemon's `module session=… turn=… <verb> <args>` log
# lines and dispatch by verb to a sub-renderer.
# ──────────────────────────────────────────────────────────────────────────

SESSION_PALETTE = (
    "#6cb6ff", "#8ed18a", "#d48ad0", "#6cd4d0",
    "#ffba6b", "#d4c46c", "#ff8a8a", "#a08aff",
)


TRUNCATED_SPAN = (
    '<span class="truncated" title="Field was truncated in the log">…</span>'
)

# (css class, glyph, label) for the four known tool_results statuses.
_TOOL_RESULT_STATUS = {
    "done":      ("done", "✓", "done"),
    "answered":  ("done", "✓", "answered"),
    "err":       ("err",  "✗", "err"),
    "no_answer": ("err",  "✗", "no answer"),
}


def format_duration_ms(ms: int) -> str:
    """Format a millisecond duration. <60s -> `X.Xs`; >=60s -> `XmYs` (Y truncated)."""
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    minutes, seconds = divmod(ms, 60_000)
    return f"{minutes}m{seconds // 1000}s"


def detect_truncated(s: str) -> tuple[str, bool]:
    """If s ends with U+2026, strip and return (prefix, True); else (s, False)."""
    if s.endswith("…"):
        return s[:-1], True
    return s, False


@functools.lru_cache(maxsize=256)
def session_color(session_id: str | None) -> str | None:
    """Hash a session id to a stable colour from SESSION_PALETTE.

    Returns None for empty / `-` / None.
    """
    if not session_id or session_id == "-":
        return None
    h = hashlib.md5(session_id.encode()).digest()[0]
    return SESSION_PALETTE[h % len(SESSION_PALETTE)]


_KEY_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")
_NEXT_KEY_RE = re.compile(r"\s+[A-Za-z_][A-Za-z0-9_]*=")


def parse_kv_args(s: str) -> dict[str, str]:
    """Parse a tail of `key=value` tokens emitted by the daemon's f-strings.

    Value forms: double/single-quoted strings, [bracket,list], and multi-word
    barewords that run until the next ` key=` boundary (or end of string).
    """
    out: dict[str, str] = {}
    i = 0
    n = len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        m = _KEY_RE.match(s, i)
        if not m:
            break
        key = m.group(1)
        i = m.end()
        if i >= n:
            out[key] = ""
            break
        ch = s[i]
        if ch in ('"', "'"):
            close = s.find(ch, i + 1)
            if close < 0:
                out[key] = s[i + 1:]
                break
            out[key] = s[i + 1: close]
            i = close + 1
        elif ch == "[":
            close = s.find("]", i + 1)
            if close < 0:
                out[key] = s[i:]
                break
            out[key] = s[i: close + 1]
            i = close + 1
        else:
            nm = _NEXT_KEY_RE.search(s, i)
            if nm is None:
                out[key] = s[i:].rstrip()
                break
            out[key] = s[i: nm.start()].rstrip()
            i = nm.start()
    return out


_TOOL_HEAD_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)#([A-Za-z0-9]+)\(")


def parse_tool_list(s: str) -> list[tuple[str, str, str]]:
    """Parse `[Name#id(args), Name#id(args)]` into a list of (name, id, args).

    Args may contain commas and closing parens; we balance parens by depth.
    """
    s = s.strip()
    if not (s.startswith("[") and s.endswith("]")):
        return []
    inner = s[1:-1]
    out: list[tuple[str, str, str]] = []
    i = 0
    n = len(inner)
    while i < n:
        while i < n and inner[i] in " ,":
            i += 1
        if i >= n:
            break
        m = _TOOL_HEAD_RE.match(inner, i)
        if not m:
            break
        name, tid = m.group(1), m.group(2)
        depth = 1
        j = m.end()
        while j < n and depth > 0:
            if inner[j] == "(":
                depth += 1
            elif inner[j] == ")":
                depth -= 1
            if depth == 0:
                break
            j += 1
        args = inner[m.end(): j]
        out.append((name, tid, args))
        i = j + 1
    return out


_RESULT_RE = re.compile(
    r"^(?P<status>done|err|answered|no_answer)\s+(?P<duration>\d+(?:\.\d+)?(?:ms|s))"
    r"(?::\s*(?P<msg>.*))?$"
)
_CONTENT_LEN_RE = re.compile(r"\s*content_len=(\d+)\s*$")


def parse_result_status(s: str) -> dict[str, str | None]:
    """Parse `done X.Xs` / `err X.Xs: <msg> [content_len=N]`."""
    m = _RESULT_RE.match(s.strip())
    if not m:
        return {"status": None, "duration": None, "msg": None, "content_len": None}
    msg = m.group("msg")
    content_len: str | None = None
    if msg:
        cm = _CONTENT_LEN_RE.search(msg)
        if cm:
            content_len = cm.group(1)
            msg = msg[: cm.start()].rstrip()
    if msg is not None and not msg.strip():
        msg = None
    return {
        "status": m.group("status"),
        "duration": m.group("duration"),
        "msg": msg,
        "content_len": content_len,
    }


_SESSION_TURN_RE = re.compile(
    r"^(?:session=(?P<session>[0-9a-fA-F-]+)\s+)?"
    r"(?:turn=(?P<turn>\d+)\s+)?"
    r"(?P<rest>.*)$",
    re.DOTALL,
)

# A subagent's assistant/user messages carry `agent=sub sub_id=… sub_type=…`
# fields right after the verb (daemon's `_subagent_fields` puts them first in
# the kv dict). Match `<verb> agent=sub sub_id=X sub_type=Y <rest>` so we can
# pull the sub_id/sub_type out and let verb dispatch see a clean tail.
_SUBAGENT_PREFIX_RE = re.compile(
    r"^(?P<verb>[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"agent=sub\s+sub_id=(?P<sub_id>\S+)\s+sub_type=(?P<sub_type>\S+)\s+"
    r"(?P<rest>.*)$",
    re.DOTALL,
)

_RUNNING_TURN_RE = re.compile(r"^Running turn (\d+):\s*(.*)$", re.DOTALL)


def _kv_span(key: str, value: str) -> str:
    """Render `key=value` as the kv chip used in entry headers and verb bodies."""
    return (
        f'<span class="kv"><span class="k">{html.escape(key)}=</span>'
        f'<span class="v">{html.escape(value)}</span></span>'
    )


def _verb_running_turn(turn: str, tail: str):
    args = parse_kv_args(tail)
    prompt = args.get("prompt", "")
    project = args.get("project", "")
    prompt_body, truncated = detect_truncated(prompt)
    ell = TRUNCATED_SPAN if truncated else ""
    chips = (
        f'<span class="chip user">prompt · turn {html.escape(turn)}</span>'
    )
    if project:
        chips += (
            '<span class="sep">·</span>'
            + _kv_span("project", project)
        )
    body = f'<div class="prompt-text">{html.escape(prompt_body)}{ell}</div>'
    return chips, body, ""


def _verb_ask_pending(tail: str):
    args = parse_kv_args(tail)
    first = args.get("first", "")
    chips = '<span class="chip ask">❓ ask user</span>'
    body = html.escape(first) if first else ""
    return chips, body, ""


def _verb_ask_answered(tail: str):
    args = parse_kv_args(tail)
    waited = args.get("waited", "")
    chips = '<span class="chip ask-out">❓ answered</span>'
    body = _kv_span("waited", waited) if waited else ""
    return chips, body, ""


def _verb_turn_interrupted(tail: str):
    args = parse_kv_args(tail)
    reason = args.get("reason", "")
    chips = '<span class="chip crit">interrupted</span>'
    body = _kv_span("reason", reason) if reason else ""
    return chips, body, ""


def _kv_or_empty(args: dict[str, str], key: str) -> str:
    v = args.get(key)
    return _kv_span(key, v) if v is not None else ""


def _verb_init(tail: str):
    """tail looks like `model=... cwd=... permission_mode=... version=... cache_ttl_policy=...`."""
    args = parse_kv_args(tail)
    chips = '<span class="chip sys">init</span>'
    parts = [
        p for p in (
            _kv_or_empty(args, "model"),
            _kv_or_empty(args, "cwd"),
            _kv_or_empty(args, "permission_mode"),
            _kv_or_empty(args, "version"),
            _kv_or_empty(args, "cache_ttl_policy"),
        ) if p
    ]
    body = '<span class="sep">·</span>'.join(parts)
    return chips, body, "t-low"


def _find_balanced_close(s: str, open_pos: int) -> int:
    """Given s[open_pos] == '[', return the index of the matching ']'."""
    depth = 0
    for i in range(open_pos, len(s)):
        c = s[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i
    return len(s) - 1


def _verb_assistant_tools(tail: str):
    """tail looks like `tools=[...] model=...`."""
    if not tail.startswith("tools="):
        return None
    rb = _find_balanced_close(tail, len("tools="))
    list_str = tail[len("tools=") : rb + 1]
    tools = parse_tool_list(list_str)
    chips = '<span class="chip tool">tool call</span>'
    rows = []
    for name, tid, args in tools:
        rows.append(
            f'<div class="tool-row">'
            f'<span class="tool-name">{html.escape(name)}</span> '
            f'<span class="tool-id">#{html.escape(tid)}</span> '
            f'<span class="tool-args">{html.escape(args)}</span>'
            f'</div>'
        )
    body = "".join(rows)
    return chips, body, ""


def _verb_thinking(tail: str):
    """tail looks like `input=N output=N cache_read=N hit=X% model=...`.

    Same body shape used for both:
      - new daemon log: `claude_runner thinking input=…` (the proper verb)
      - legacy log:     `claude_runner assistant input=…` (older daemons that
        labelled thinking-only AssistantMessages as plain `assistant`)
    """
    args = parse_kv_args(tail)
    chips = '<span class="chip reply">thinking</span>'
    parts = [
        p for p in (
            _kv_or_empty(args, "input"),
            _kv_or_empty(args, "output"),
            _kv_or_empty(args, "cache_read"),
            _kv_or_empty(args, "hit"),
        ) if p
    ]
    body = '<span class="sep">·</span>'.join(parts)
    return chips, body, ""


def _verb_assistant_text(tail: str):
    """tail looks like `"text content..."` (already quote-wrapped)."""
    text = tail.strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        text = text[1:-1]
    body_text, truncated = detect_truncated(text)
    ell = TRUNCATED_SPAN if truncated else ""
    chips = '<span class="chip reply">text</span>'
    body = (
        f'<div class="assistant-text">{html.escape(body_text)}{ell}</div>'
    )
    return chips, body, ""


def _verb_assistant_meta(tail: str):
    """tail looks like `text_len=N text_preview="..." model=... stop_reason=... error=...`.

    AssistantMessage variants that carry a text payload alongside non-trivial
    metadata (stop_reason, error) — the pure-text branch (``_verb_assistant_text``)
    only fires when the daemon dropped the metadata. The most operationally
    important case here is the synthetic API-error reply (``error=server_error``
    with a 5xx body in text_preview); promote that to the critical chip family
    so it can't be missed in a long log scroll.
    """
    args = parse_kv_args(tail)
    text_len = args.get("text_len", "")
    preview = args.get("text_preview", "")
    model = args.get("model", "")
    stop_reason = args.get("stop_reason", "")
    error = args.get("error", "")

    if error:
        chips = '<span class="chip crit">⚠️ assistant error</span>'
    else:
        chips = '<span class="chip reply">text</span>'
    for k, v in (
        ("error", error),
        ("stop_reason", stop_reason),
        ("model", model),
        ("text_len", text_len),
    ):
        if v:
            chips += '<span class="sep">·</span>' + _kv_span(k, v)

    body_text, truncated = detect_truncated(preview)
    if body_text.startswith('"') and body_text.endswith('"') and len(body_text) >= 2:  # pragma: no cover
        body_text = body_text[1:-1]
    ell = TRUNCATED_SPAN if truncated else ""
    body = (
        f'<div class="assistant-text">{html.escape(body_text)}{ell}</div>'
        if body_text else ""
    )
    return chips, body, ""


def _verb_user_text(tail: str):
    """tail looks like `text_len=N text_preview="..."`.

    UserMessage can carry TextBlock content (Skill output, system reminder
    injections, the subagent-report payload). Rendered as a blue-outline reply
    chip with the text_preview shown as an assistant-text-like block.
    """
    args = parse_kv_args(tail)
    preview = args.get("text_preview", "")
    text_len = args.get("text_len", "")
    chips = '<span class="chip reply-out">text</span>'
    # text_len is a magnitude signal — useful when the payload is much larger
    # than the 80-char preview (3KB skill banner vs. one-line reminder), but
    # noise when the preview already shows the whole thing. Threshold matches
    # roughly 2.5× the preview length.
    try:
        if int(text_len) > 200:
            chips += '<span class="sep">·</span>' + _kv_span("text_len", text_len)
    except ValueError:
        pass
    body_text, truncated = detect_truncated(preview)
    if body_text.startswith('"') and body_text.endswith('"') and len(body_text) >= 2:  # pragma: no cover
        # parse_kv_args already strips the surrounding quote pair the daemon
        # writes; this is a defensive second pass for the rare double-wrapped
        # case (never observed in production logs).
        body_text = body_text[1:-1]
    ell = TRUNCATED_SPAN if truncated else ""
    body = f'<div class="assistant-text">{html.escape(body_text)}{ell}</div>'
    return chips, body, ""


def _verb_tool_results(tail: str):
    """tail looks like `tool_results=[Tool#id(done|err X.Xs[: msg [content_len=N]])]`."""
    prefix = "tool_results="
    if not tail.startswith(prefix):
        return None
    rb = _find_balanced_close(tail, len(prefix))
    list_str = tail[len(prefix) : rb + 1]
    items = parse_tool_list(list_str)
    chips = '<span class="chip tool-out">tool result</span>'
    rows = []
    for name, tid, status_str in items:
        parsed = parse_result_status(status_str)
        status = parsed["status"]
        duration = parsed["duration"] or ""
        msg = parsed["msg"]
        clen = parsed["content_len"]
        # Legacy compat: pre-fix daemons logged AskUserQuestion tool_results
        # as `err` because the SDK marks PermissionResultDeny as is_error=True
        # — even though the user did answer. Promote based on the message
        # prefix so historical lines render the same as new ones.
        if name == "AskUserQuestion" and status == "err" and msg:
            if msg.startswith("The user answered"):
                status = "answered"
            elif msg.startswith("The user did not answer"):
                status = "no_answer"
        status_view = _TOOL_RESULT_STATUS.get(status)
        if status_view:
            css, glyph, label = status_view
            head = (
                f'<span class="duration {css}">{glyph} {label} {html.escape(duration)}</span>'
            )
        else:
            head = f'<span class="duration">{html.escape(status_str)}</span>'
        clen_html = (
            f'<span class="sep">·</span>' + _kv_span("content_len", clen)
            if clen else ""
        )
        row = (
            f'<div class="tool-row">'
            f'<span class="tool-name">{html.escape(name)}</span> '
            f'<span class="tool-id">#{html.escape(tid)}</span> '
            f'{head}{clen_html}'
            f'</div>'
        )
        if msg:
            msg_body, truncated = detect_truncated(msg)
            ell = TRUNCATED_SPAN if truncated else ""
            row += (
                f'<div class="body-line muted">{html.escape(msg_body)}{ell}</div>'
            )
        rows.append(row)
    body = "".join(rows)
    return chips, body, ""


def _verb_permission_denied(tail: str):
    """tail looks like `tool_name=... tool_use_id=... decision_reason_type=... decision_reason=<multi-word> message=<multi-word>`."""
    args = parse_kv_args(tail)
    chips = '<span class="chip crit">🚫 permission denied</span>'

    def _row(label: str, value: str) -> str:
        v, truncated = detect_truncated(value)
        ell = TRUNCATED_SPAN if truncated else ""
        return (
            f'<div class="row"><span class="label">{html.escape(label)}</span>'
            f'{html.escape(v)}{ell}</div>'
        )

    tool = args.get("tool_name", "")
    use_id = args.get("tool_use_id", "")
    rtype = args.get("decision_reason_type", "")
    reason = args.get("decision_reason", "")
    msg = args.get("message", "")
    tool_html = (
        f'<span class="tool-name">{html.escape(tool)}</span> '
        f'<span class="tool-id">{html.escape(use_id)}</span>'
        if tool or use_id else ""
    )
    body = (
        '<div class="deny-block">'
        f'<div class="row"><span class="label">tool</span>{tool_html}</div>'
        f'{_row("reason type", rtype)}'
        f'{_row("reason", reason)}'
        f'{_row("message", msg)}'
        '</div>'
    )
    return chips, body, ""


def _verb_result(tail: str):
    """tail looks like `subtype=... duration_ms=... num_turns=... cost=$... permission_denials=[...] result="..."`."""
    args = parse_kv_args(tail)
    subtype = args.get("subtype", "")
    cost = args.get("cost", "")
    duration_ms = args.get("duration_ms", "")
    num_turns = args.get("num_turns", "")
    denials = args.get("permission_denials", "")
    result_text = args.get("result", "")

    # success → assistant-reply family (solid blue, paired visually with the
    # `text` and `thinking` chips). Any other subtype is an aborted turn —
    # promote to the critical family (solid red).
    if not subtype or subtype == "success":
        chip_cls = "reply"
        chip_label = "turn done · success" if subtype else "turn done"
    else:
        chip_cls = "crit"
        chip_label = f"turn err · {subtype}"
    chips = f'<span class="chip {chip_cls}">{html.escape(chip_label)}</span>'

    summary_parts: list[str] = []
    if duration_ms.isdigit():
        summary_parts.append(html.escape(format_duration_ms(int(duration_ms))))
    if num_turns:
        summary_parts.append(f"{html.escape(num_turns)} turns")
    if denials and denials != "[]":
        summary_parts.append(f"denied: {html.escape(denials)}")
    if summary_parts:
        chips += (
            '<span class="sep">·</span>'
            f'<span class="muted">{" · ".join(summary_parts)}</span>'
        )

    body = ""
    if result_text:
        rt, truncated = detect_truncated(result_text)
        ell = TRUNCATED_SPAN if truncated else ""
        body = f'<div class="result-text">{html.escape(rt)}{ell}</div>'
    return chips, body, ""


def _verb_turn_tokens(tail: str):
    """tail looks like `input=N output=N cache_read=N hit=X% write_1h=N write_5m=N`."""
    args = parse_kv_args(tail)
    chips = '<span class="chip reply-out">turn tokens</span>'
    parts = [
        _kv_span(k, args[k])
        for k in ("input", "output", "cache_read", "hit", "write_1h")
        if k in args
    ]
    body = '<span class="sep">·</span>'.join(parts)
    return chips, body, ""


def _verb_task_started(tail: str):
    """tail looks like `task_id=... subagent_type=... desc=...`."""
    args = parse_kv_args(tail)
    tid = args.get("task_id", "")[:8]
    sub = args.get("subagent_type", "")
    desc = args.get("desc", "")
    chips = '<span class="chip task">task ▶</span>'  # solid cyan — start
    if tid:
        chips += '<span class="sep">·</span>' + _kv_span("task_id", tid)
    if sub:
        chips += '<span class="sep">·</span>' + _kv_span("subagent_type", sub)
    body = html.escape(desc) if desc else ""
    return chips, body, ""


def _verb_task_notification(tail: str):
    """tail looks like `task_id=... status=... duration_ms=... tool_uses=... total_tokens=...`.

    `tool_uses` keeps its source-field name in the chip rather than collapsing
    to `tools` — the assistant's own tool-call list also uses `tools`, and the
    two reading side-by-side were easy to confuse. `total_tokens` is a raw
    integer in the log; route it through format_tokens for a compact form.
    """
    args = parse_kv_args(tail)
    tid = args.get("task_id", "")[:8]
    status = args.get("status", "")
    duration_ms = args.get("duration_ms", "")
    tool_uses = args.get("tool_uses", "")
    tokens = args.get("total_tokens", "")
    chips = '<span class="chip task-out">task ●</span>'
    if tid:
        chips += '<span class="sep">·</span>' + _kv_span("task_id", tid)
    if status:
        chips += '<span class="sep">·</span>' + _kv_span("status", status)
    if duration_ms.isdigit():
        chips += '<span class="sep">·</span>' + _kv_span(
            "duration", format_duration_ms(int(duration_ms))
        )
    if tool_uses:
        chips += '<span class="sep">·</span>' + _kv_span("tool_uses", tool_uses)
    if tokens:
        try:
            tokens_fmt = format_tokens(int(tokens))
        except ValueError:
            tokens_fmt = tokens
        chips += '<span class="sep">·</span>' + _kv_span("tokens", tokens_fmt)
    return chips, "", ""


_RATE_RESETS_LOCAL_RE = re.compile(r"\((\d{4}-\d{2}-\d{2}[^)]*)\)")


def _verb_rate_limit(tail: str):
    """tail looks like `status=... type=... utilization=X% resets_at=<unix> (YYYY-MM-DD HH:MM:SS)`."""
    args = parse_kv_args(tail)
    util = args.get("utilization", "")
    rl_type = args.get("type", "")
    resets = args.get("resets_at", "")
    chips = (
        f'<span class="chip crit">⚠️ rate limit · {html.escape(util)}</span>'
    )
    summary: list[str] = []
    if rl_type:
        summary.append(html.escape(rl_type))
    if resets:
        # Prefer the human-readable local time the daemon already formatted
        # inside the parens; drop the raw unix epoch prefix.
        m = _RATE_RESETS_LOCAL_RE.search(resets)
        pretty = m.group(1) if m else resets
        summary.append(f"resets={html.escape(pretty)}")
    if summary:
        chips += (
            '<span class="sep">·</span>'
            f'<span class="muted">{" · ".join(summary)}</span>'
        )
    return chips, "", ""


def _verb_system(tail: str):
    """tail looks like `subtype=... cache_ttl_policy=...`."""
    args = parse_kv_args(tail)
    subtype = args.get("subtype", "")
    chips = (
        f'<span class="chip sys">system · {html.escape(subtype)}</span>'
        if subtype else '<span class="chip sys">system</span>'
    )
    return chips, "", "t-low"


def _verb_daemon_inbound(tail: str):
    """tail looks like `msgtype=... sender=... preview="..."`."""
    args = parse_kv_args(tail)
    msgtype = args.get("msgtype", "")
    sender = args.get("sender", "")
    preview = args.get("preview", "")
    chips = (
        f'<span class="chip user">📥 inbound · {html.escape(msgtype)}</span>'
    )
    if sender:
        chips += '<span class="sep">·</span>' + _kv_span("sender", sender)
    body = (
        f'<span class="preview">{html.escape(preview)}</span>'
        if preview else ""
    )
    return chips, body, ""


def _dispatch_verb(module: str, rest: str):
    """Dispatch by (module, verb) to a sub-renderer.

    Returns (chips_html, body_html, extra_class) on a hit, or None when no
    verb branch claims this entry — caller falls back to raw <pre>.

    Future bundles register verb handlers by adding branches below.
    """
    # Low-priority plumbing: WebSocket / startup chatter is rendered as raw
    # text but dimmed via t-low so it never competes with real events.
    if module == "client":
        return "", f'<pre class="raw">{html.escape(rest)}</pre>', "t-low"
    if module == "daemon":
        if rest.startswith("Starting"):
            return "", f'<pre class="raw">{html.escape(rest)}</pre>', "t-low"
        if rest.startswith("inbound"):
            return _verb_daemon_inbound(rest[len("inbound"):].lstrip())
    if module == "orchestrator":
        m = _RUNNING_TURN_RE.match(rest)
        if m:
            return _verb_running_turn(m.group(1), m.group(2))
        if rest.startswith("ask_user_question answered"):
            return _verb_ask_answered(rest[len("ask_user_question answered"):].lstrip())
        if rest.startswith("ask_user_question"):
            return _verb_ask_pending(rest[len("ask_user_question"):].lstrip())
        if rest.startswith("turn interrupted"):
            return _verb_turn_interrupted(rest[len("turn interrupted"):].lstrip())
    if module == "claude_runner":
        if rest.startswith("init "):
            return _verb_init(rest[len("init "):])
        if rest.startswith("assistant tools="):
            return _verb_assistant_tools(rest[len("assistant "):])
        if rest.startswith("thinking "):
            return _verb_thinking(rest[len("thinking "):])
        # Legacy: pre-`thinking`-verb daemons logged thinking-only snapshots
        # as `assistant input=…`. Route them to the same renderer so old logs
        # render with the same chip as new ones.
        if rest.startswith("assistant input="):
            return _verb_thinking(rest[len("assistant "):])
        if rest.startswith('assistant "'):
            return _verb_assistant_text(rest[len("assistant "):])
        # Same family — assistant text/thinking with extra metadata. Routed
        # last so the cheap quoted-string and tools= branches above stay hot.
        if rest.startswith("assistant text_len=") or rest.startswith(
            "assistant thinking_len="
        ):
            return _verb_assistant_meta(rest[len("assistant "):])
        if rest.startswith("user tool_results="):
            return _verb_tool_results(rest[len("user "):])
        if rest.startswith("user text_len="):
            return _verb_user_text(rest[len("user "):])
        if rest.startswith("permission_denied"):
            return _verb_permission_denied(rest[len("permission_denied"):].lstrip())
        if rest.startswith("result subtype="):
            return _verb_result(rest[len("result "):])
        if rest.startswith("turn tokens:"):
            return _verb_turn_tokens(rest[len("turn tokens:"):].lstrip())
        if rest.startswith("task_started"):
            return _verb_task_started(rest[len("task_started"):].lstrip())
        if rest.startswith("task_notification"):
            return _verb_task_notification(rest[len("task_notification"):].lstrip())
        if rest.startswith("rate_limit_event"):
            return _verb_rate_limit(rest[len("rate_limit_event"):].lstrip())
        if rest.startswith("system "):
            return _verb_system(rest[len("system "):])
    return None


def _render_entry_shell(
    idx: int,
    ts: str,
    level: str,
    module: str,
    session_id: str | None,
    turn: str | None,
    sub_id: str | None,
    sub_type: str | None,
    chips_html: str,
    body_html: str,
    extra_class: str,
    raw_line: str,
) -> str:
    """Wrap a sub-renderer's pieces in the canonical entry card.

    Header layout (left to right):
      #idx | YYYY-MM-DD HH:MM:SS | LEVEL | module | session=… · turn=… |
      chips | (sub: <8char> · <type>)

    The original log line is stashed in `data-raw` so a ctrl/cmd+click on the
    header can swap the rendered body for the raw text (see the inline JS).
    """
    tint = session_color(session_id)
    style = f' style="--session-tint: {tint}"' if tint else ""
    class_list = ["entry"]
    if extra_class:
        class_list.append(extra_class)

    ctx_parts: list[str] = []
    if session_id:
        ctx_parts.append(_kv_span("session", session_id))
    if turn:
        ctx_parts.append(_kv_span("turn", turn))
    ctx_html = '<span class="sep">·</span>'.join(ctx_parts) if ctx_parts else ""

    # Subagent annotation — when a verb came from inside a Task subagent, mark
    # it with a task-family outline chip and indent the whole entry so the
    # operator can spot it at a glance and visually pair it with its
    # parent `task ▶` line. sub_id rendered as first 8 chars to match the
    # convention used elsewhere in the daemon.
    sub_annot = ""
    if sub_id or sub_type:
        bits = []
        if sub_type:
            bits.append(html.escape(sub_type))
        if sub_id:
            bits.append(html.escape(sub_id[:8]))
        sub_annot = (
            '<span class="sep">·</span>'
            f'<span class="chip task-out">sub · {" · ".join(bits)}</span>'
        )
        class_list.append("sub")
    classes = " ".join(class_list)

    header = (
        f'<header>'
        f'<span class="ln">#{idx}</span>'
        f'<span class="ts">{html.escape(ts)}</span>'
        f'<span class="lvl">{html.escape(level)}</span>'
        f'<span class="mod">{html.escape(module)}</span>'
        f'{ctx_html}'
        f'{chips_html}'
        f'{sub_annot}'
        f'</header>'
    )
    body = f'<div class="body">{body_html}</div>' if body_html else ""
    raw_attr = f' data-raw="{html.escape(raw_line, quote=True)}"'
    return (
        f'<article class="{classes}" id="L{idx}"{style}{raw_attr}>'
        f'{header}{body}</article>'
    )


def render_entry(idx: int, ts: str, level: str, module: str, rest: str) -> str:
    raw_line = f"{ts} {level} {module} {rest}"

    # 1. SDK branch (reserved).
    m = SDK_MSG_RE.match(rest)
    if m:
        return _render_sdk_message(
            idx, ts, level, module, m.group("kind"), m.group("payload"), raw_line,
        )

    # 2. Strip optional `session=… turn=…` prefix.
    pm = _SESSION_TURN_RE.match(rest)
    session_id = pm.group("session") if pm else None
    turn = pm.group("turn") if pm else None
    # `session=-` is the sentinel for "no real session" — treat as missing.
    if session_id == "-":
        session_id = None
    body_rest = pm.group("rest") if pm else rest

    # 2b. If this message came from inside a subagent, the daemon's
    # `_subagent_fields` injected `agent=sub sub_id=… sub_type=…` right after
    # the verb. Pull those out so the verb dispatcher sees a clean tail and
    # the shell can annotate the header with the sub id/type.
    sub_id: str | None = None
    sub_type: str | None = None
    sm = _SUBAGENT_PREFIX_RE.match(body_rest)
    if sm:
        sub_id = sm.group("sub_id")
        sub_type = sm.group("sub_type")
        body_rest = f"{sm.group('verb')} {sm.group('rest')}"

    # 3. Verb dispatch (future bundles fill in branches). None → raw fallback.
    verb_html = _dispatch_verb(module, body_rest)
    if verb_html is not None:
        chips_html, body_html, extra_class = verb_html
    else:
        chips_html = ""
        body_html = f'<pre class="raw">{html.escape(body_rest)}</pre>'
        extra_class = ""

    return _render_entry_shell(
        idx, ts, level, module, session_id, turn, sub_id, sub_type,
        chips_html, body_html, extra_class, raw_line,
    )


CSS = """
:root {
  --bg: #0f1115; --panel: #151821; --panel2: #1b1f2a;
  --fg: #e6e9ef; --muted: #8a93a6; --accent: #6cb6ff;
  --border: #262b38;
  --topbar: 49px;
  --t-assistant: #2a3f5f; --t-user: #2d4a36;
  --t-system: #3f3a2a; --t-hook: #4a2d44;
  --t-result: #5a4a1f; --t-task: #2a4a4a; --t-rate: #4a2a2a;
  --t-thinking: #3a2a4a; --t-text: #2a3a4a;
  --t-tooluse: #2a4a2a; --t-toolresult: #4a3a2a;
  --t-default: #2c3140;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
}
#top {
  position: sticky; top: 0; z-index: 30;
  background: var(--panel); border-bottom: 1px solid var(--border);
  padding: 12px 20px; display: flex; gap: 16px; align-items: center;
  height: var(--topbar);
}
#top h1 { margin: 0; font-size: 16px; font-weight: 600; }
#top .meta { color: var(--muted); font-size: 12px; }
#top input[type=search] {
  background: var(--bg); border: 1px solid var(--border); color: var(--fg);
  padding: 6px 10px; border-radius: 4px; min-width: 240px; font: inherit;
}
#top button {
  background: var(--panel2); border: 1px solid var(--border); color: var(--fg);
  padding: 6px 10px; border-radius: 4px; cursor: pointer; font: inherit;
}
#top button:hover { border-color: var(--accent); }

main { padding: 16px 20px; max-width: 1400px; margin: 0 auto; }

.entry {
  background: var(--panel); border: 1px solid var(--border);
  border-left: 4px solid var(--t-default);
  border-radius: 6px; margin-bottom: 12px;
}
.entry header {
  position: sticky; top: var(--topbar); z-index: 5;
  padding: 6px 12px; display: flex; gap: 10px; align-items: baseline;
  background: #1a1d27; border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
  border-top-right-radius: 6px;
}
.entry .ln { color: var(--muted); width: 40px; }
.entry .ts { color: var(--muted); }
.entry .lvl { color: #9ad48a; font-weight: 600; }
.entry .mod { color: var(--accent); }
.entry .kind { color: #ffba6b; font-weight: 600; }
.entry .body { padding: 10px 12px; }

.entry.t-assistant { border-left-color: #6cb6ff; }
.entry.t-user { border-left-color: #8ed18a; }
.entry.t-system { border-left-color: #d4c46c; }
.entry.t-hook { border-left-color: #d48ad0; }
.entry.t-result { border-left-color: #ffba6b; }
.entry.t-task { border-left-color: #6cd4d0; }
.entry.t-rate { border-left-color: #ff8a8a; }

details.obj {
  background: var(--panel2); border: 1px solid var(--border);
  border-radius: 4px; padding: 0; margin: 4px 0;
}
details.obj > summary {
  cursor: pointer; padding: 6px 10px; user-select: none;
  display: flex; gap: 8px; flex-wrap: wrap; align-items: baseline;
}
details.obj > summary:hover { background: rgba(255,255,255,0.03); }
details.obj > table { width: 100%; border-collapse: collapse; }
details.obj > table th {
  text-align: left; vertical-align: top;
  padding: 4px 10px; color: var(--accent); font-weight: normal;
  width: 180px; border-top: 1px solid var(--border);
}
details.obj > table td {
  padding: 4px 10px; border-top: 1px solid var(--border);
  word-break: break-word; max-width: 0;
}

details.obj.t-assistant { background: linear-gradient(to right, var(--t-assistant) 0 4px, var(--panel2) 4px); }
details.obj.t-user { background: linear-gradient(to right, var(--t-user) 0 4px, var(--panel2) 4px); }
details.obj.t-system { background: linear-gradient(to right, var(--t-system) 0 4px, var(--panel2) 4px); }
details.obj.t-hook { background: linear-gradient(to right, var(--t-hook) 0 4px, var(--panel2) 4px); }
details.obj.t-result { background: linear-gradient(to right, var(--t-result) 0 4px, var(--panel2) 4px); }
details.obj.t-task { background: linear-gradient(to right, var(--t-task) 0 4px, var(--panel2) 4px); }
details.obj.t-thinking { background: linear-gradient(to right, var(--t-thinking) 0 4px, var(--panel2) 4px); }
details.obj.t-text { background: linear-gradient(to right, var(--t-text) 0 4px, var(--panel2) 4px); }
details.obj.t-tooluse { background: linear-gradient(to right, var(--t-tooluse) 0 4px, var(--panel2) 4px); }
details.obj.t-toolresult { background: linear-gradient(to right, var(--t-toolresult) 0 4px, var(--panel2) 4px); }

.type { color: #ffba6b; font-weight: 600; }
.summary { color: var(--muted); font-style: italic; }
.count { color: var(--muted); font-size: 11px; }

ol.list { margin: 4px 0; padding-left: 24px; }
ol.list > li { margin: 4px 0; }
table.plain {
  width: 100%; border-collapse: collapse;
  background: var(--panel2); border: 1px solid var(--border); border-radius: 4px;
}
table.plain th {
  text-align: left; vertical-align: top;
  padding: 4px 10px; color: var(--accent); font-weight: normal;
  width: 180px; border-top: 1px solid var(--border);
}
table.plain th:first-child, table.plain td:first-child { border-top: none; }
table.plain td { padding: 4px 10px; border-top: 1px solid var(--border); word-break: break-word; }

.lit.null { color: #ff8a8a; }
.lit.bool { color: #d48ad0; }
.lit.num { color: #ffba6b; }
.str-inline { color: #9ad48a; }
pre.str {
  background: #0a0c10; border: 1px solid var(--border); border-radius: 4px;
  padding: 8px 10px; margin: 4px 0; color: #cfe3c4; white-space: pre-wrap;
  word-break: break-word; max-height: 400px; overflow: auto;
}
pre.raw {
  background: #0a0c10; border: 1px solid var(--border); border-radius: 4px;
  padding: 8px 10px; margin: 0; color: var(--fg); white-space: pre-wrap;
  word-break: break-word;
}
.empty { color: var(--muted); }
.badge {
  background: var(--accent); color: #0a0c10; padding: 1px 6px;
  border-radius: 3px; font-size: 11px; font-weight: 600;
}
details.json-embed { margin: 4px 0; }
details.json-embed > summary { cursor: pointer; padding: 2px 0; }


/* ── Structured renderer additions ──────────────────────────────────── */

:root {
  /* Six verb families. Solid = primary event, outline = follow-up/meta. */
  --fam-user:   #a08aff;   /* purple — messages originating from the user */
  --fam-ask:    #d4c46c;   /* yellow — questions to the human (and answers) */
  --fam-crit:   #ff8a8a;   /* red    — permission_denied / interrupt / rate-limit / turn err */
  --fam-tool:   #8ed18a;   /* green  — assistant ↔ tools round-trip */
  --fam-reply:  #6cb6ff;   /* blue   — assistant's own outputs (text, thinking, turn done, usage) */
  --fam-task:   #6cd4d0;   /* cyan   — subagent / task lifecycle */
  --fam-sys:    #6a7080;   /* gray   — init / system / connection plumbing */

  /* Legacy aliases — body blocks and inline tokens still use these names;
     mapping them to families keeps everything coherent without rewriting
     each rule. */
  --c-prompt: var(--fam-user);
  --c-text:   var(--fam-reply);
  --c-tool:   var(--fam-tool);
  --c-deny:   var(--fam-crit);
  --c-done:   var(--fam-tool);
  --c-err:    var(--fam-crit);
  --c-result: #ffba6b;     /* still used by .cost; orange retained for that */
}

.entry { border-left-color: var(--session-tint, var(--t-default)); }

/* Unified inline separator. Always muted; never inherits a neighbour colour. */
.sep { color: var(--muted); margin: 0 4px; }

/* Truncated-field marker. Dotted underline + tooltip on hover. */
.truncated {
  text-decoration: underline dotted var(--muted);
  text-underline-offset: 2px;
  cursor: help;
}

/* Verb chip in header. */
.chip {
  display: inline-block; padding: 1px 8px; border-radius: 3px;
  font-size: 10px; font-weight: 700; letter-spacing: 0.04em;
  text-transform: uppercase;
  background: var(--chip-bg, var(--panel2));
  color: var(--chip-fg, var(--fg));
  border: 1px solid var(--chip-border, transparent);
}
/* Solid chips — primary event in family */
.chip.user   { --chip-bg: var(--fam-user);   --chip-fg: #0a0c10; }
.chip.ask    { --chip-bg: var(--fam-ask);    --chip-fg: #0a0c10; }
.chip.crit   { --chip-bg: var(--fam-crit);   --chip-fg: #0a0c10; }
.chip.tool   { --chip-bg: var(--fam-tool);   --chip-fg: #0a0c10; }
.chip.reply  { --chip-bg: var(--fam-reply);  --chip-fg: #0a0c10; }
.chip.task   { --chip-bg: var(--fam-task);   --chip-fg: #0a0c10; }
/* Outline chips — follow-up / meta within a family */
.chip.ask-out  { --chip-bg: transparent; --chip-fg: var(--fam-ask);   --chip-border: var(--fam-ask); }
.chip.tool-out { --chip-bg: transparent; --chip-fg: var(--fam-tool);  --chip-border: var(--fam-tool); }
.chip.reply-out{ --chip-bg: transparent; --chip-fg: var(--fam-reply); --chip-border: var(--fam-reply); }
.chip.task-out { --chip-bg: transparent; --chip-fg: var(--fam-task);  --chip-border: var(--fam-task); }
/* System / plumbing — gray outline, paired with .entry.t-low body dimming */
.chip.sys      { --chip-bg: transparent; --chip-fg: var(--fam-sys);   --chip-border: var(--fam-sys); }

/* Inline kv chip rendering (e.g. `session=03cdbc4a turn=1` in the header). */
.kv .k { color: #555c70; }
.kv .v { color: var(--fg); }

/* Tool call/result rendering. */
.tool-name { color: var(--c-tool); font-weight: 600; }
.tool-id   { color: var(--muted); font-size: 11px; }
.tool-args {
  color: #cfe3c4; background: #0a0c10; padding: 1px 6px;
  border-radius: 3px; border: 1px solid var(--border);
}
.duration       { color: var(--muted); }
.duration.done  { color: var(--c-done); }
.duration.err   { color: var(--c-err); }
.cost           { color: var(--c-result); font-weight: 600; }

/* Body text blocks. */
.prompt-text {
  color: #e6e9ef; padding: 4px 8px;
  background: rgba(160, 138, 255, 0.08);
  border-left: 2px solid var(--c-prompt);
  border-radius: 0 3px 3px 0;
  white-space: pre-wrap;
}
.assistant-text {
  color: #cfd9ff; padding: 4px 8px;
  background: rgba(108, 182, 255, 0.05);
  border-left: 2px solid var(--c-text);
  border-radius: 0 3px 3px 0;
  white-space: pre-wrap;
}
.result-text {
  color: #d8e8ff; padding: 4px 8px;
  background: rgba(108, 182, 255, 0.05);
  border-left: 2px solid var(--fam-reply);
  border-radius: 0 3px 3px 0;
  white-space: pre-wrap;
}
.deny-block {
  background: rgba(255, 138, 138, 0.07);
  border-left: 2px solid var(--c-deny);
  padding: 6px 8px; border-radius: 0 3px 3px 0;
}
.deny-block .row { padding: 1px 0; }
.deny-block .row .label { color: var(--c-deny); width: 130px; display: inline-block; }
.preview { color: #cfe3c4; font-style: italic; }

/* Low-priority entries (client / daemon plumbing). Single class so token /
   usage rows stay at full brightness — never use .t-low on usage. */
.entry.t-low { opacity: 0.55; }

/* Sub-agent entries indent so their nesting under a `task ▶` is visible at a
   glance; combined with the task-out `sub · …` chip in the header that's
   enough to spot subagent activity in a long scroll. */
.entry.sub { margin-left: 28px; }

/* `collapse all` hides the body; only the sticky header remains visible. */
.entry.collapsed > .body { display: none; }

/* ctrl/cmd+click on a header toggles this state; body becomes raw text. */
.entry header { cursor: default; }
.entry.raw-view header { background: #20242f; }
/* Match body's base font / line-height exactly and zero out pre's UA padding
   so toggling doesn't visibly resize most entries. Body's own padding
   (10px 12px) still surrounds the raw line. */
.raw-line {
  margin: 0; padding: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px; line-height: 1.5;
  color: var(--fg);
  white-space: pre-wrap; word-break: break-word;
}

.entry.hidden { display: none; }

/* Verb / chip legend modal */
#legend-modal {
  position: fixed; inset: 0; z-index: 100;
  background: rgba(0, 0, 0, 0.55);
  display: flex; align-items: flex-start; justify-content: center;
  padding: 40px 20px;
  overflow-y: auto;
}
#legend-modal.hidden { display: none; }
.legend-card {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px;
  max-width: 880px; width: 100%;
  color: var(--fg);
}
.legend-card header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px; border-bottom: 1px solid var(--border);
}
.legend-card header h2 { margin: 0; font-size: 15px; font-weight: 600; }
.legend-card header button {
  background: transparent; border: 1px solid var(--border); color: var(--muted);
  border-radius: 4px; padding: 4px 10px; cursor: pointer; font: inherit;
}
.legend-card header button:hover { color: var(--fg); border-color: var(--accent); }
.legend-intro {
  margin: 12px 18px; color: var(--muted); font-size: 12px; line-height: 1.6;
}
.legend-intro code, .legend-intro kbd {
  background: var(--panel2); border: 1px solid var(--border);
  padding: 1px 5px; border-radius: 3px; color: var(--fg); font-size: 11px;
}
.legend-table {
  width: calc(100% - 36px); margin: 0 18px 12px;
  border-collapse: collapse; font-size: 12px;
}
.legend-table th, .legend-table td {
  padding: 8px 10px; vertical-align: top; text-align: left;
  border-top: 1px solid var(--border);
}
.legend-table th {
  background: var(--panel2); color: var(--accent); font-weight: normal;
}
.legend-table code {
  background: var(--panel2); border: 1px solid var(--border);
  padding: 1px 5px; border-radius: 3px; color: var(--fg); font-size: 11px;
}
.legend-table .family-cell { white-space: nowrap; font-weight: 600; }
.legend-table .chip-cell {
  /* Wide enough for the longest chip (`turn err · error_max_turns`) to stay
     on a single line; the description column happily takes the remainder. */
  min-width: 260px; width: 260px;
}
.legend-table .chip-cell .chip-row {
  padding: 3px 0;
  white-space: nowrap;
}
.legend-table .chip-cell .chip-row + .chip-row {
  margin-top: 2px;
}
.legend-swatch {
  display: inline-block; width: 10px; height: 10px;
  border-radius: 2px; margin-right: 6px; vertical-align: middle;
  border: 1px solid transparent;
}
.legend-swatch-outline { background: transparent; border-width: 2px; }
"""


# --- Live tail watcher -----------------------------------------------------

POLL_INTERVAL = 0.2
# split_with_remainder always holds back the last LINE_RE-matched line as
# leftover, in case continuation lines arrive in a later poll. If nothing
# else is ever logged the last line sits in the buffer forever — flush it
# once it has been static for this long.
LEFTOVER_FLUSH_AFTER = 1.5
DEFAULT_TAIL_BYTES = 256 * 1024
LOAD_EARLIER_CHUNK = 256 * 1024
# Initial backlog cap on the in-memory deque. A heavy day's daemon log can
# emit a few thousand entries; at 2000 the earliest of today's entries would
# silently fall off the deque and — because the `--since today` lower-bound
# byte was already at file start — the JS "load earlier ↑" button would hide
# itself thinking everything was already on screen. 10000 covers a busy day
# with ample headroom; worst-case memory is ~50 MB.
BACKLOG_MAX = 10000


# --- Parsing helpers -------------------------------------------------------


def parse_ts(s: str | None, upper: bool = False) -> str | None:
    """Normalize 'YYYY-MM-DD[ HH:MM[:SS]]' (or ISO-T) to 'YYYY-MM-DD HH:MM:SS'.

    Missing time bits default to 00:00:00 for since-bounds and 23:59:59 for
    until-bounds, so `--until 2026-05-23` covers the whole day.
    """
    if not s:
        return None
    s = s.strip().replace("T", " ")
    parts = s.split(" ", 1)
    date = parts[0]
    if len(parts) == 1:
        time_str = "23:59:59" if upper else "00:00:00"
    else:
        tparts = parts[1].split(":")
        defaults = ["59", "59"] if upper else ["00", "00"]
        while len(tparts) < 3:
            tparts.append(defaults[len(tparts) - 1])
        time_str = ":".join(p.zfill(2) for p in tparts)
    return f"{date} {time_str}"


def split_entries(
    text: str, start_idx: int = 1
) -> Iterator[tuple[int, str, str, str, str]]:
    buf: list[str] = []
    idx = start_idx - 1
    for line in text.splitlines():
        if LINE_RE.match(line):
            if buf:
                m = LINE_RE.match("\n".join(buf))
                if m:
                    idx += 1
                    yield (idx, m["ts"], m["level"], m["module"], m["rest"])
            buf = [line]
        else:
            if buf:
                buf.append(line)
    if buf:
        m = LINE_RE.match("\n".join(buf))
        if m:
            idx += 1
            yield (idx, m["ts"], m["level"], m["module"], m["rest"])


def split_with_remainder(
    text: str, start_idx: int
) -> tuple[list[tuple[int, str, str, str, str]], str]:
    lines = text.splitlines(keepends=True)
    last_start = -1
    for i, line in enumerate(lines):
        if LINE_RE.match(line):
            last_start = i
    if last_start < 0:
        return [], text
    return list(split_entries("".join(lines[:last_start]), start_idx)), "".join(
        lines[last_start:]
    )


def render_entry_payload(entry: tuple[int, str, str, str, str]) -> dict:
    idx, ts, level, module, rest = entry
    return {"idx": idx, "html": render_entry(idx, ts, level, module, rest)}


# --- Binary search ---------------------------------------------------------


def _next_entry_at(f: BinaryIO, pos: int, size: int) -> tuple[str | None, int]:
    """Return (ts, line_pos) of the first LINE_RE-matching line at or after pos."""
    f.seek(pos)
    if pos > 0:
        f.readline()  # discard partial line
    while True:
        line_pos = f.tell()
        if line_pos >= size:
            return None, size
        line = f.readline()
        if not line:
            return None, size
        m = LINE_RE.match(line.decode("utf-8", errors="replace"))
        if m:
            return m["ts"], line_pos


def find_byte_for_ts(path: Path, target_ts: str, strict: bool = False) -> int:
    """Lower-bound byte offset: first entry with ts >= target (or > if strict).

    Returns file size if no such entry exists. Uses ``hi = mid`` so the search
    window always halves, even when individual entries span tens of KB on one
    physical line and ``_next_entry_at`` would otherwise return a position
    equal to ``hi``.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return 0
    if size == 0:
        return 0
    lo, hi = 0, size
    with path.open("rb") as f:
        while lo < hi:
            mid = (lo + hi) // 2
            ts, line_pos = _next_entry_at(f, mid, size)
            if ts is None or line_pos >= hi:
                # No entry found within the current window; narrow.
                hi = mid
                continue
            cmp = (ts > target_ts) if strict else (ts >= target_ts)
            if cmp:
                hi = mid
            else:
                # Skip past this entry's first line. line_pos+1 guarantees
                # progress; the next iteration's readline() realigns.
                lo = line_pos + 1
        _, line_pos = _next_entry_at(f, lo, size)
        return line_pos


# --- Tail watcher ----------------------------------------------------------


class FileTail:
    def __init__(
        self,
        path: Path,
        tail_bytes: int,
        since: str | None = None,
        until: str | None = None,
        source_label: str = "out",
    ):
        self.path = path
        self.tail_bytes = tail_bytes
        self.since = since
        self.until = until
        self.source = source_label
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._next_idx = 1
        self._leftover = ""
        self._pos = 0
        self._inode: int | None = None
        self._backlog: collections.deque[dict] = collections.deque(maxlen=BACKLOG_MAX)
        self._tail_start_byte = 0
        self._lower_bound_byte = 0  # never load earlier than this
        self._terminal = False  # stop polling (until is fully in the past)
        self._leftover_changed_at: float = 0.0

    def _filter_entries(
        self, entries: list[tuple[int, str, str, str, str]]
    ) -> list[tuple[int, str, str, str, str]]:
        if not self.since and not self.until:
            return entries
        out = []
        for e in entries:
            _, ts, *_ = e
            if self.since and ts < self.since:
                continue
            if self.until and ts > self.until:
                continue
            out.append(e)
        return out

    def initialize(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return
        self._inode = st.st_ino
        size = st.st_size
        if size == 0:
            return
        # Decide where the initial backlog window starts.
        until_byte: int | None = None
        if self.since:
            start = find_byte_for_ts(self.path, self.since)
            self._lower_bound_byte = start
        elif self.until:
            # Anchor to until: read tail_bytes ending at first entry > until.
            until_byte = find_byte_for_ts(self.path, self.until, strict=True)
            start = max(0, until_byte - self.tail_bytes)
        else:
            start = max(0, size - self.tail_bytes)
        with self.path.open("rb") as f:
            f.seek(start)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        if start > 0 and not self.since:
            # Drop partial first line so we don't merge into a prior entry.
            nl = text.find("\n")
            if nl >= 0:
                text = text[nl + 1 :]
                start += nl + 1
        self._tail_start_byte = start
        self._pos = size
        entries = list(split_entries(text, self._next_idx))
        # Renumber after filtering so backlog idx is contiguous.
        filtered = self._filter_entries(entries)
        for i, e in enumerate(filtered, start=self._next_idx):
            idx, ts, lvl, mod, rest = e
            self._backlog.append(render_entry_payload((i, ts, lvl, mod, rest)))
        if filtered:
            self._next_idx += len(filtered)
        # find_byte_for_ts returns size when no entry exceeds until — so a
        # value < size means the file already extends past the bound and the
        # polling loop has nothing more to wait for.
        if until_byte is not None and until_byte < size:
            self._terminal = True

    def read_range(self, start: int, end: int) -> list[dict]:
        try:
            with self.path.open("rb") as f:
                f.seek(start)
                data = f.read(end - start)
        except FileNotFoundError:
            return []
        text = data.decode("utf-8", errors="replace")
        if start > 0:
            nl = text.find("\n")
            if nl >= 0:
                text = text[nl + 1 :]
        entries = list(split_entries(text))
        filtered = self._filter_entries(entries)
        return [
            render_entry_payload((-(start + i), ts, lvl, mod, rest))
            for i, (_, ts, lvl, mod, rest) in enumerate(filtered)
        ]

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=BACKLOG_MAX * 2)
        with self._lock:
            q.put_nowait(
                {
                    "event": "meta",
                    "source": self.source,
                    "tail_start_byte": self._tail_start_byte,
                    "lower_bound_byte": self._lower_bound_byte,
                    "file_size": self._pos,
                    "since": self.since,
                    "until": self.until,
                    "terminal": self._terminal,
                }
            )
            for payload in self._backlog:
                q.put_nowait({"event": "entry", "source": self.source, "payload": payload})
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def start(self) -> None:
        if self._terminal:
            return
        threading.Thread(
            target=self._loop, name=f"log-tail-{self.source}", daemon=True
        ).start()

    def _broadcast(self, msg: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    def _maybe_flush_leftover(self) -> None:
        # Only flush a complete single-line entry: a multi-line entry split
        # across polls would leave the continuation lines orphaned.
        if not self._leftover or not self._leftover.endswith("\n"):
            return
        if time.monotonic() - self._leftover_changed_at < LEFTOVER_FLUSH_AFTER:
            return
        lines = self._leftover.splitlines(keepends=True)
        if len(lines) != 1 or not LINE_RE.match(lines[0]):
            return
        entries = list(split_entries(self._leftover, self._next_idx))
        if len(entries) != 1:
            return
        self._leftover = ""
        filtered = self._filter_entries(entries)
        if not filtered:
            return
        _, ts, lvl, mod, rest = filtered[0]
        with self._lock:
            payload = render_entry_payload((self._next_idx, ts, lvl, mod, rest))
            self._next_idx += 1
            self._backlog.append(payload)
            self._broadcast(
                {"event": "entry", "source": self.source, "payload": payload}
            )

    def _loop(self) -> None:
        while True:
            time.sleep(POLL_INTERVAL)
            try:
                st = self.path.stat()
            except FileNotFoundError:
                continue
            if self._inode is not None and (
                st.st_ino != self._inode or st.st_size < self._pos
            ):
                self._inode = st.st_ino
                self._pos = 0
                self._leftover = ""
                with self._lock:
                    self._backlog.clear()
                    self._tail_start_byte = 0
                    self._lower_bound_byte = 0
                    self._broadcast({"event": "rotated", "source": self.source})
            if st.st_size <= self._pos:
                self._maybe_flush_leftover()
                continue
            with self.path.open("rb") as f:
                f.seek(self._pos)
                data = f.read(st.st_size - self._pos)
            self._pos = st.st_size
            text = self._leftover + data.decode("utf-8", errors="replace")
            entries, leftover = split_with_remainder(text, self._next_idx)
            self._leftover = leftover
            self._leftover_changed_at = time.monotonic()
            filtered = self._filter_entries(entries)
            with self._lock:
                # If until is set and any entry now exceeds it, mark terminal
                # so we stop polling after this batch.
                if self.until and entries and entries[-1][1] > self.until:
                    self._terminal = True
                renumbered = []
                for e in filtered:
                    _, ts, lvl, mod, rest = e
                    renumbered.append((self._next_idx, ts, lvl, mod, rest))
                    self._next_idx += 1
                for e in renumbered:
                    payload = render_entry_payload(e)
                    self._backlog.append(payload)
                    self._broadcast(
                        {"event": "entry", "source": self.source, "payload": payload}
                    )
                if self._terminal:
                    self._broadcast({"event": "ended", "source": self.source})
            if self._terminal:
                return


# --- HTTP handler ----------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    tails: dict[str, FileTail]
    file_paths: dict[str, str]
    since: str | None
    until: str | None
    index_body: bytes

    def log_message(self, fmt, *args):
        return

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def _source(self, query: dict) -> str:
        s = query.get("source", ["out"])[0]
        return s if s in self.tails else "out"

    def _send_json(self, body: dict, status: int = 200) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/favicon.png", "/favicon.ico"):
            if ICON_BYTES is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(ICON_BYTES)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(ICON_BYTES)
            return
        if parsed.path == "/":
            body = self.index_body
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/earlier":
            qs = parse_qs(parsed.query)
            src = self._source(qs)
            tail = self.tails[src]
            try:
                before = int(qs.get("before", ["0"])[0])
            except ValueError:
                before = 0
            lower = tail._lower_bound_byte
            start = max(lower, before - LOAD_EARLIER_CHUNK)
            payloads = tail.read_range(start, before) if before > lower else []
            self._send_json(
                {
                    "entries": payloads,
                    "new_before": start,
                    "exhausted": start <= lower,
                }
            )
            return
        if parsed.path == "/events":
            qs = parse_qs(parsed.query)
            src = self._source(qs)
            self._serve_sse(self.tails[src])
            return
        self.send_response(404)
        self.end_headers()

    def _serve_sse(self, tail: FileTail) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = tail.subscribe()
        try:
            last_beat = time.time()
            while True:
                try:
                    msg = q.get(timeout=1.0)
                except queue.Empty:
                    msg = None
                if msg is not None:
                    payload = json.dumps(msg)
                    try:
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                if time.time() - last_beat > 15:
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    last_beat = time.time()
        finally:
            tail.unsubscribe(q)


# --- HTML / JS client ------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Trace daemon logs for claude-dingtalk-bridge - live</title>
<link rel="icon" type="image/png" href="/favicon.png">
<style>
__CSS__
/* live-viewer additions */
#top .pill { background: var(--panel2); border: 1px solid var(--border);
  padding: 4px 10px; border-radius: 999px; color: var(--muted); font-size: 12px; }
#top .pill.live { color: #9ad48a; border-color: #2e5b32; }
#top .pill.paused { color: #ffba6b; border-color: #5b4a2a; }
#top .pill.error { color: #ff8a8a; border-color: #5b2a2a; }
#top .range { color: var(--muted); font-size: 12px; }

/* Two-row top bar: title/status/range/toggle on row 1, filter + buttons on row 2. */
#top { display: block; height: auto; padding: 10px 20px; }
#top .row {
  display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
}
#top .row + .row { margin-top: 8px; }
#top .row input[type=search] { flex: 1 1 280px; max-width: 600px; }

#source-toggle {
  margin-left: auto;
  display: inline-flex;
  border: 1px solid var(--border); border-radius: 6px; overflow: hidden;
}
#source-toggle .src {
  background: var(--panel2); border: none; border-radius: 0;
  color: var(--muted); padding: 6px 12px; cursor: pointer; font: inherit;
  display: inline-flex; align-items: center; gap: 6px;
  border-right: 1px solid var(--border);
}
#source-toggle .src:last-child { border-right: none; }
#source-toggle .src.active { background: var(--accent); color: #0a0c10; }
#source-toggle .src:hover:not(.active) { color: var(--fg); }
#source-toggle .src .badge {
  background: var(--accent); color: #0a0c10; border-radius: 999px;
  padding: 0 8px; font-size: 11px; font-weight: 600; min-width: 18px; text-align: center;
}
#source-toggle .src.active .badge { background: #0a0c10; color: var(--accent); }
#source-toggle .src[data-tab="err"] .badge { background: #ff8a8a; color: #0a0c10; }
#source-toggle .src[data-tab="err"].active { background: #ff8a8a; }
#source-toggle .src[data-tab="err"].active .badge { background: #0a0c10; color: #ff8a8a; }
#source-toggle .src .badge.zero { display: none; }

.pane { display: none; }
.pane.active { display: block; }

.pane .earlier-row { text-align: center; margin: 12px 0; }
.pane .earlier-row button { padding: 6px 16px; }
.pane .earlier-row .done { color: var(--muted); font-style: italic; }
</style>
</head><body>
<div id="top">
  <div class="row">
    <h1>Trace daemon logs</h1>
    <span id="status" class="pill live">connecting…</span>
    <span id="follow" class="pill live">follow: on</span>
    <span class="range" id="range"></span>
    <div id="source-toggle">
      <button class="src active" data-tab="out">stdout<span class="badge zero">0</span></button>
      <button class="src" data-tab="err">stderr<span class="badge zero">0</span></button>
    </div>
  </div>
  <div class="row">
    <input id="q" type="search" placeholder="filter (active source)…">
    <button id="expand">expand all</button>
    <button id="collapse">collapse all</button>
    <button id="clear">clear view</button>
    <button id="legend" title="Verb / chip legend">verbs info</button>
  </div>
</div>
<div id="legend-modal" class="hidden" aria-hidden="true">
  <div class="legend-card" role="dialog" aria-label="Verb chip legend">
    <header>
      <h2>Verb chips</h2>
      <button id="legend-close" aria-label="close">✕</button>
    </header>
    <p class="legend-intro">
      Each log entry is classified by its <code>verb</code> and rendered with
      a chip from one of 7 colour families. Solid = primary event in the
      family; outline = follow-up or meta within the same family.
    </p>
    <table class="legend-table">
      <thead>
        <tr><th>Family</th><th>Chip</th><th>Used by</th></tr>
      </thead>
      <tbody>
        <tr><td class="family-cell"><span class="legend-swatch" style="background:var(--fam-user)"></span>User</td>
            <td class="chip-cell">
              <div class="chip-row"><span class="chip user">📥 inbound · text</span></div>
              <div class="chip-row"><span class="chip user">prompt · turn 3</span></div>
            </td>
            <td>User → daemon. The user just sent a message (<code>daemon inbound</code>) or kicked off a new turn for the assistant (<code>orchestrator Running turn N</code>).</td></tr>

        <tr><td class="family-cell"><span class="legend-swatch" style="background:var(--fam-ask)"></span>Ask</td>
            <td class="chip-cell">
              <div class="chip-row"><span class="chip ask">❓ ask user</span></div>
            </td>
            <td>Assistant → user. The assistant is asking the user a question and waiting on an answer (<code>ask_user_question</code>).</td></tr>
        <tr><td class="family-cell"></td>
            <td class="chip-cell">
              <div class="chip-row"><span class="chip ask-out">❓ answered</span></div>
            </td>
            <td>User → assistant. The user has replied to the pending question (<code>ask_user_question answered</code>).</td></tr>

        <tr><td class="family-cell"><span class="legend-swatch" style="background:var(--fam-crit)"></span>Critical</td>
            <td class="chip-cell">
              <div class="chip-row"><span class="chip crit">🚫 permission denied</span></div>
              <div class="chip-row"><span class="chip crit">interrupted</span></div>
              <div class="chip-row"><span class="chip crit">⚠️ rate limit · 95%</span></div>
              <div class="chip-row"><span class="chip crit">turn err · error_max_turns</span></div>
              <div class="chip-row"><span class="chip crit">⚠️ assistant error</span></div>
            </td>
            <td>Events that warrant attention: <code>permission_denied</code>, <code>turn interrupted</code>, <code>rate_limit_event</code>, <code>result subtype</code> other than <code>success</code>, and <code>AssistantMessage</code> with an <code>error=...</code> field (typically a synthetic API-error reply with a 5xx body).</td></tr>

        <tr><td class="family-cell"><span class="legend-swatch" style="background:var(--fam-tool)"></span>Tool</td>
            <td class="chip-cell">
              <div class="chip-row"><span class="chip tool">tool call</span></div>
            </td>
            <td>Assistant → tool. The assistant is invoking a tool (<code>assistant tools=[...]</code>).</td></tr>
        <tr><td class="family-cell"></td>
            <td class="chip-cell">
              <div class="chip-row"><span class="chip tool-out">tool result</span></div>
            </td>
            <td>Tool → assistant. A tool returned its result (<code>user tool_results=[...]</code>; the <code>user</code> role here is the SDK's wire-format convention for tool replies, not the human user).</td></tr>

        <tr><td class="family-cell"><span class="legend-swatch" style="background:var(--fam-reply)"></span>Reply</td>
            <td class="chip-cell">
              <div class="chip-row"><span class="chip reply">text</span></div>
              <div class="chip-row"><span class="chip reply">thinking</span></div>
              <div class="chip-row"><span class="chip reply">turn done · success</span></div>
            </td>
            <td>Assistant → user. The assistant's own outputs: a text reply (<code>assistant "text..."</code>), a thinking phase (<code>thinking</code>), or a clean turn close (<code>result subtype=success</code>).</td></tr>
        <tr><td class="family-cell"></td>
            <td class="chip-cell">
              <div class="chip-row"><span class="chip reply-out">turn tokens</span></div>
              <div class="chip-row"><span class="chip reply-out">text</span></div>
            </td>
            <td>Assistant-side meta and running totals: per-turn token aggregate (<code>turn tokens:</code>) and the SDK's <code>user</code>-role messages carrying a TextBlock — text fed <em>into</em> the assistant from a non-human source (Skill output, system reminder injection, subagent report). Same label as the solid <code>text</code> chip above; the outline style marks the opposite direction.</td></tr>

        <tr><td class="family-cell"><span class="legend-swatch" style="background:var(--fam-task)"></span>Task</td>
            <td class="chip-cell">
              <div class="chip-row"><span class="chip task">task ▶</span></div>
            </td>
            <td>Assistant → subagent. The assistant spawned a subagent (<code>task_started</code>).</td></tr>
        <tr><td class="family-cell"></td>
            <td class="chip-cell">
              <div class="chip-row"><span class="chip task-out">task ●</span></div>
            </td>
            <td>Subagent → assistant. The subagent finished and reported back (<code>task_notification</code>).</td></tr>
        <tr><td class="family-cell"></td>
            <td class="chip-cell">
              <div class="chip-row"><span class="chip task-out">sub · general · 1a2b3c4d</span></div>
            </td>
            <td>Annotation, not a verb. Layered onto any entry that originated from inside a subagent (paired with the parent <code>task ▶</code> above); the entry is also indented so it groups visually.</td></tr>

        <tr><td class="family-cell"><span class="legend-swatch legend-swatch-outline" style="border-color:var(--fam-sys)"></span>System</td>
            <td class="chip-cell">
              <div class="chip-row"><span class="chip sys">init</span></div>
              <div class="chip-row"><span class="chip sys">system · api_retry</span></div>
            </td>
            <td>Daemon plumbing: SDK startup, retries, WebSocket / launchd noise — <code>init</code>, <code>system</code>, <code>client *</code>, <code>daemon Starting</code>. The whole entry is dimmed via <code>t-low</code>.</td></tr>
      </tbody>
    </table>
    <p class="legend-intro">
      Tips: <code>Command + click</code> an entry's header to toggle between
      the rendered view and the raw log line;
      <code>expand all</code> / <code>collapse all</code> folds every entry's
      body; the 4px left border colour is hashed from the session id so
      entries from the same session share a colour.
    </p>
  </div>
</div>
<main>
  <section class="pane active" id="pane-out">
    <div class="earlier-row"><button class="earlier" data-source="out">load earlier ↑</button></div>
    <div class="entries"></div>
  </section>
  <section class="pane" id="pane-err">
    <div class="earlier-row"><button class="earlier" data-source="err">load earlier ↑</button></div>
    <div class="entries"></div>
  </section>
</main>
<script>
// Keep in sync with BACKLOG_MAX on the Python side: the SSE initial-flush
// can deliver up to BACKLOG_MAX entries in one go, and a tighter DOM cap
// here causes the oldest ones to be removed as the newer ones land — the
// page visibly "flashes" old entries then settles at the cutoff.
const MAX_DOM = 10000;
const SINCE = "__SINCE__", UNTIL = "__UNTIL__";

// Show range in header if present.
const rangeEl = document.getElementById('range');
if (SINCE || UNTIL) {
  rangeEl.textContent = 'range: '
    + (SINCE ? ('since ' + SINCE) : '−∞')
    + ' → '
    + (UNTIL ? UNTIL : 'live');
} else {
  rangeEl.remove();
}

const statusEl = document.getElementById('status');
const followEl = document.getElementById('follow');
const search = document.getElementById('q');

const tabs = {
  out: makeTab('out'),
  err: makeTab('err'),
};
let currentTab = 'out';
let following = true;
let connections = { out: false, err: false };

function makeTab(source) {
  const pane = document.getElementById('pane-' + source);
  return {
    source,
    pane,
    entriesEl: pane.querySelector('.entries'),
    earlierRow: pane.querySelector('.earlier-row'),
    earlierBtn: pane.querySelector('.earlier'),
    tabBtn: document.querySelector('#source-toggle .src[data-tab="' + source + '"]'),
    badge: document.querySelector('#source-toggle .src[data-tab="' + source + '"] .badge'),
    earliestByte: null,
    lowerBoundByte: 0,
    unread: 0,
    firstBatch: true,
    scrollPos: 0,
    ended: false,
  };
}

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = 'pill ' + cls;
}
function setFollow(on) {
  following = on;
  followEl.textContent = 'follow: ' + (on ? 'on' : 'off');
  followEl.className = 'pill ' + (on ? 'live' : 'paused');
}
function isNearBottom() {
  return (window.innerHeight + window.scrollY) >= (document.body.scrollHeight - 80);
}
function applyFilter(node) {
  const q = search.value.toLowerCase().trim();
  if (!q) { node.classList.remove('hidden'); return; }
  node.classList.toggle('hidden', !node.textContent.toLowerCase().includes(q));
}
function bumpBadge(tab) {
  tab.unread += 1;
  tab.badge.textContent = tab.unread;
  tab.badge.classList.remove('zero');
}
function clearBadge(tab) {
  tab.unread = 0;
  tab.badge.textContent = '0';
  tab.badge.classList.add('zero');
}
function appendEntry(tab, payload, position) {
  const tpl = document.createElement('template');
  tpl.innerHTML = payload.html.trim();
  const node = tpl.content.firstElementChild;
  if (!node) return;
  applyFilter(node);
  if (position === 'start') tab.entriesEl.insertBefore(node, tab.entriesEl.firstChild);
  else tab.entriesEl.appendChild(node);
  if (position === 'end' && tab.entriesEl.childElementCount > MAX_DOM) {
    tab.entriesEl.removeChild(tab.entriesEl.firstElementChild);
  }
}

function switchTab(source) {
  if (source === currentTab) return;
  tabs[currentTab].scrollPos = window.scrollY;
  tabs[currentTab].pane.classList.remove('active');
  tabs[currentTab].tabBtn.classList.remove('active');
  currentTab = source;
  tabs[currentTab].pane.classList.add('active');
  tabs[currentTab].tabBtn.classList.add('active');
  clearBadge(tabs[source]);
  // Restore this tab's saved scroll, deferring to next frame so layout settles.
  requestAnimationFrame(() => window.scrollTo(0, tabs[source].scrollPos));
}

document.querySelectorAll('#source-toggle .src').forEach(b => {
  b.addEventListener('click', () => switchTab(b.dataset.tab));
});

// Debounce: with MAX_DOM=10k entries each keystroke would otherwise force a
// full querySelectorAll + textContent walk over every card.
let searchTimer = null;
search.addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    const tab = tabs[currentTab];
    const q = search.value.toLowerCase().trim();
    tab.entriesEl.querySelectorAll('.entry').forEach(e => {
      if (!q) { e.classList.remove('hidden'); return; }
      e.classList.toggle('hidden', !e.textContent.toLowerCase().includes(q));
    });
  }, 150);
});
// expand/collapse work on two kinds of foldables:
//   - <details> nodes inside SDK-message entries (the typed-tree renderer)
//   - .entry cards in the new verb renderer: `collapsed` hides the body,
//     leaving only the sticky header so users can skim long sessions
function setAllCollapsed(collapsed) {
  // Single tree walk covers both <details> (anywhere in the subtree) and the
  // .entry card itself — avoids a second pass over 10k nodes.
  const root = tabs[currentTab].entriesEl;
  root.querySelectorAll('.entry, .entry details').forEach(el => {
    if (el.tagName === 'DETAILS') el.open = !collapsed;
    else el.classList.toggle('collapsed', collapsed);
  });
}
document.getElementById('expand').onclick = () => setAllCollapsed(false);
document.getElementById('collapse').onclick = () => setAllCollapsed(true);
document.getElementById('clear').onclick = () => { tabs[currentTab].entriesEl.innerHTML = ''; };

// Verb-chip legend modal.
const legendModal = document.getElementById('legend-modal');
function showLegend() {
  legendModal.classList.remove('hidden');
  legendModal.setAttribute('aria-hidden', 'false');
}
function hideLegend() {
  legendModal.classList.add('hidden');
  legendModal.setAttribute('aria-hidden', 'true');
}
document.getElementById('legend').onclick = showLegend;
document.getElementById('legend-close').onclick = hideLegend;
// Click on the dim backdrop (but not on the card itself) closes the modal.
legendModal.addEventListener('click', (e) => {
  if (e.target === legendModal) hideLegend();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !legendModal.classList.contains('hidden')) hideLegend();
});

window.addEventListener('scroll', () => {
  if (isNearBottom() && !following) setFollow(true);
  else if (!isNearBottom() && following) setFollow(false);
}, { passive: true });

function refreshConnectionStatus() {
  const states = Object.values(connections);
  if (states.every(Boolean)) setStatus('live', 'live');
  else if (states.some(Boolean)) setStatus('partial', 'paused');
  else setStatus('reconnecting…', 'error');
}

function openStream(source) {
  const tab = tabs[source];
  const es = new EventSource('/events?source=' + source);
  es.onopen = () => { connections[source] = true; refreshConnectionStatus(); };
  es.onerror = () => { connections[source] = false; refreshConnectionStatus(); };
  es.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.event === 'meta') {
      tab.earliestByte = msg.tail_start_byte;
      tab.lowerBoundByte = msg.lower_bound_byte || 0;
      if (tab.earliestByte <= tab.lowerBoundByte) {
        tab.earlierRow.innerHTML = '<span class="done">'
          + (tab.lowerBoundByte > 0 ? '— since boundary —' : '— start of file —')
          + '</span>';
      }
      if (msg.terminal) markEnded(tab);
      return;
    }
    if (msg.event === 'rotated') {
      tab.entriesEl.innerHTML = '';
      tab.earliestByte = 0;
      tab.lowerBoundByte = 0;
      tab.earlierRow.innerHTML = '<span class="done">— rotated, start of new file —</span>';
      tab.firstBatch = true;
      return;
    }
    if (msg.event === 'ended') {
      markEnded(tab);
      return;
    }
    if (msg.event === 'entry') {
      const onActiveTab = (currentTab === source);
      const wasAtBottom = onActiveTab && (isNearBottom() || tab.firstBatch);
      appendEntry(tab, msg.payload, 'end');
      if (!onActiveTab) bumpBadge(tab);
      if (following && wasAtBottom) {
        requestAnimationFrame(() => window.scrollTo(0, document.body.scrollHeight));
      }
    }
  };
  setTimeout(() => { tab.firstBatch = false; }, 500);
}

function markEnded(tab) {
  if (tab.ended) return;
  tab.ended = true;
  // Optionally surface in UI later. For now leave silent.
}

document.querySelectorAll('.earlier').forEach(btn => {
  btn.addEventListener('click', async () => {
    const tab = tabs[btn.dataset.source];
    if (tab.earliestByte === null || tab.earliestByte <= tab.lowerBoundByte) return;
    btn.disabled = true;
    btn.textContent = 'loading…';
    const before = tab.earliestByte;
    const r = await fetch('/earlier?source=' + tab.source + '&before=' + before);
    const data = await r.json();
    const prevHeight = document.body.scrollHeight;
    const prevScroll = window.scrollY;
    for (let i = data.entries.length - 1; i >= 0; i--) {
      appendEntry(tab, data.entries[i], 'start');
    }
    const newHeight = document.body.scrollHeight;
    window.scrollTo(0, prevScroll + (newHeight - prevHeight));
    tab.earliestByte = data.new_before;
    if (data.exhausted) {
      tab.earlierRow.innerHTML = '<span class="done">'
        + (tab.lowerBoundByte > 0 ? '— since boundary —' : '— start of file —')
        + '</span>';
    } else {
      btn.disabled = false;
      btn.textContent = 'load earlier ↑';
    }
  });
});

// ctrl/cmd+click on an entry header swaps the rendered body for the raw
// log line (held in data-raw). Click again with the modifier to revert.
// Delegated on document so entries inserted via SSE or "load earlier" pick
// it up automatically.
document.addEventListener('click', (e) => {
  if (!(e.ctrlKey || e.metaKey)) return;
  const header = e.target.closest && e.target.closest('.entry > header');
  if (!header) return;
  const entry = header.parentElement;
  if (!entry || !entry.hasAttribute('data-raw')) return;
  e.preventDefault();
  let body = entry.querySelector(':scope > .body');
  if (entry.classList.contains('raw-view')) {
    if (body && entry._origBody !== undefined) body.innerHTML = entry._origBody;
    entry.classList.remove('raw-view');
  } else {
    if (!body) {
      body = document.createElement('div');
      body.className = 'body';
      entry.appendChild(body);
      entry._origBody = '';
    } else {
      entry._origBody = body.innerHTML;
    }
    const pre = document.createElement('pre');
    pre.className = 'raw-line';
    pre.textContent = entry.getAttribute('data-raw') || '';
    body.innerHTML = '';
    body.appendChild(pre);
    entry.classList.add('raw-view');
    // Showing raw text on a collapsed card would have nothing visible to
    // toggle, so lift the collapse. Reverting raw-view leaves the entry
    // expanded — the user already chose to look at it.
    entry.classList.remove('collapsed');
  }
});

// Keep the --topbar CSS var in sync with the actual top-bar height, so the
// sticky entry headers always sit just below it even when row 2 wraps.
const topEl = document.getElementById('top');
function syncTopbarVar() {
  const h = topEl.getBoundingClientRect().height;
  document.documentElement.style.setProperty('--topbar', h + 'px');
}
syncTopbarVar();
new ResizeObserver(syncTopbarVar).observe(topEl);

openStream('out');
openStream('err');
</script>
</body></html>
"""


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=str(DEFAULT_OUT), help="stdout log file path")
    p.add_argument("--err", default=str(DEFAULT_ERR), help="stderr log file path")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--tail-bytes", type=int, default=DEFAULT_TAIL_BYTES)
    p.add_argument(
        "--since",
        default=None,
        help='Lower bound ts, e.g. "2026-05-23" or "2026-05-23 10:00:00"',
    )
    p.add_argument(
        "--until",
        default=None,
        help='Upper bound ts, e.g. "2026-05-23" (=> end of day, inclusive)',
    )
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()

    since = parse_ts(args.since, upper=False)
    until = parse_ts(args.until, upper=True)
    # When neither bound is given, default to today's logs (00:00:00 local).
    # Pass an explicit --since (e.g. "2000-01-01") to override.
    if since is None and until is None:
        since = parse_ts(datetime.date.today().isoformat())
    if since and until and since > until:
        sys.exit(f"--since ({since}) is after --until ({until})")

    out_path = Path(args.out).expanduser().resolve()
    err_path = Path(args.err).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.touch(exist_ok=True)
    err_path.touch(exist_ok=True)

    tails = {
        "out": FileTail(out_path, args.tail_bytes, since, until, "out"),
        "err": FileTail(err_path, args.tail_bytes, since, until, "err"),
    }
    for t in tails.values():
        t.initialize()
        t.start()

    Handler.tails = tails
    Handler.file_paths = {"out": str(out_path), "err": str(err_path)}
    Handler.since = since
    Handler.until = until
    Handler.index_body = (
        INDEX_HTML.replace("__CSS__", CSS)
        .replace("__SINCE__", since or "")
        .replace("__UNTIL__", until or "")
        .encode("utf-8")
    )

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            sys.exit(
                f"port {args.port} is already in use on 127.0.0.1.\n"
                f"another log-web server may be running — try:\n"
                f"  lsof -nP -iTCP:{args.port} -sTCP:LISTEN\n"
                f"or pass a different port:\n"
                f"  make logs-web ARGS=\"--port <other>\""
            )
        raise
    url = f"http://127.0.0.1:{args.port}/"
    print(f"serving at {url}")
    print(f"  stdout: {out_path}")
    print(f"  stderr: {err_path}")
    if since or until:
        print(f"  range: {since or '-inf'} → {until or 'live'}")
    print("(Ctrl+C to quit)")
    if not args.no_open:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
