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

    DEFAULT_OUT = _LOG_DIR / "daemon.out.log"
    DEFAULT_ERR = _LOG_DIR / "daemon.err.log"
except Exception:
    _LOG_DIR = Path.home() / "Library/Logs/claude-dingtalk-bridge"
    DEFAULT_OUT = _LOG_DIR / "daemon.out.log"
    DEFAULT_ERR = _LOG_DIR / "daemon.err.log"

# Favicon — read once at startup; package resource. None if unavailable.
ICON_BYTES: bytes | None = None
try:
    import claude_dingtalk_bridge as _pkg  # noqa: E402

    _icon = Path(_pkg.__file__).parent / "resources" / "icon.png"
    if _icon.exists():
        ICON_BYTES = _icon.read_bytes()
except Exception:
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
        if node.id in ("True", "False", "None"):
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


def render_entry(idx: int, ts: str, level: str, module: str, rest: str) -> str:
    m = SDK_MSG_RE.match(rest)
    if m:
        kind = m.group("kind")
        payload_text = m.group("payload")
        obj = parse_repr(payload_text)
        cls = TYPE_CLASS.get(kind, "t-default")
        if obj is None:
            body = f'<pre class="raw">{html.escape(payload_text)}</pre>'
        else:
            body = render_value(obj)
        return (
            f'<article class="entry sdk {cls}" id="L{idx}">'
            f'<header><span class="ln">#{idx}</span>'
            f'<span class="ts">{ts}</span>'
            f'<span class="lvl">{level}</span>'
            f'<span class="mod">{module}</span>'
            f'<span class="kind">{kind}</span></header>'
            f'<div class="body">{body}</div></article>'
        )
    return (
        f'<article class="entry plain" id="L{idx}">'
        f'<header><span class="ln">#{idx}</span>'
        f'<span class="ts">{ts}</span>'
        f'<span class="lvl">{level}</span>'
        f'<span class="mod">{module}</span></header>'
        f'<div class="body"><pre class="raw">{html.escape(rest)}</pre></div>'
        f'</article>'
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
  padding: 8px 12px; display: flex; gap: 12px; align-items: baseline;
  background: #1a1d27; border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
  border-top-left-radius: 2px; border-top-right-radius: 6px;
  margin: -1px -1px 0 -1px;
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

.entry.hidden { display: none; }
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
BACKLOG_MAX = 2000


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
        # If until is set and the file's tail is already past it, stop polling.
        if self.until:
            with self.path.open("rb") as f:
                ts, _ = _next_entry_at(f, max(0, size - 4096), size)
            if ts is not None and ts > self.until:
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
            body = (
                INDEX_HTML.replace("__CSS__", CSS)
                .replace("__SINCE__", self.since or "")
                .replace("__UNTIL__", self.until or "")
                .encode("utf-8")
            )
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
const MAX_DOM = 1000;
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

search.addEventListener('input', () => {
  const tab = tabs[currentTab];
  const q = search.value.toLowerCase().trim();
  tab.entriesEl.querySelectorAll('.entry').forEach(e => {
    if (!q) { e.classList.remove('hidden'); return; }
    e.classList.toggle('hidden', !e.textContent.toLowerCase().includes(q));
  });
});
document.getElementById('expand').onclick = () =>
  tabs[currentTab].entriesEl.querySelectorAll('details').forEach(d => d.open = true);
document.getElementById('collapse').onclick = () =>
  tabs[currentTab].entriesEl.querySelectorAll('details').forEach(d => d.open = false);
document.getElementById('clear').onclick = () => { tabs[currentTab].entriesEl.innerHTML = ''; };

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

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
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
