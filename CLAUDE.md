# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A macOS daemon that lets you drive Claude Code on your computer from your phone
via DingTalk. It connects to DingTalk in Stream mode (outbound-only), so no
public IP or tunnel is needed. The phone sends prompts and control commands;
the daemon runs Claude Code turns and escalates risky operations back to the
phone for approval.

## Commands

All daily operations are wrapped in the `Makefile` — run `make` with no args to
list them. Key ones:

- `make setup` — create `.venv` and `pip install -e ".[dev]"`
- `make test` — run the test suite (`pytest -q`)
- `make start` — run the daemon in the foreground (logs to terminal)
- `make daemon-install` / `daemon-uninstall` / `daemon-start` / `daemon-stop` /
  `daemon-restart` / `daemon-status` — manage the launchd service
- `make logs-tail` — `tail -f` the daemon out/err logs in the terminal
- `make logs-web` — browser-based live log viewer with tabs and `--since`/`--until`
  date-range filtering (`make logs-web ARGS="--since 2026-05-23"`)

Run a single test: `.venv/bin/pytest tests/test_orchestrator.py -q` (or append
`::test_name`). Tests use `pytest-asyncio` in `auto` mode — async test
functions need no decorator.

## Architecture

Inbound messages and outbound messages travel separate paths:

- **Inbound**: `dingtalk_stream` opens a persistent WebSocket; `daemon._ChatHandler`
  receives chatbot messages. `text` goes straight to the orchestrator; `audio`
  messages forward DingTalk's own transcription; `picture`/`richText` messages
  have their images downloaded (`images.download_image`) and assembled into a
  prompt (`build_image_prompt`) before the turn runs.
- **Outbound**: `DingTalkTransport` (`dingtalk.py`) calls the DingTalk Open API
  REST endpoints to push 1:1 messages, managing its own access token. It is
  sync/`requests`-based and called from async code via `asyncio.to_thread`.

`daemon.build_orchestrator` wires everything together. Note the circular
reference resolved by assignment: the `ClaudeRunner` needs the orchestrator's
`request_permission` and `answer_question` as its `permission_handler` /
`question_handler`, so both are set after the two objects exist.

### Orchestrator (`orchestrator.py`) — the core

Single-threaded async coordinator. Holds all mutable session state:

- One task runs at a time (`self._task`). New prompts arriving mid-task are
  queued (`self._queue`) and drained sequentially when the task finishes. The
  exception: a new prompt arriving while the runner is in the post-turn drain
  phase (`runner.is_draining`) calls `cancel_drain()` and starts immediately,
  rather than waiting out a possibly-stuck background-agent wait.
- Control commands are parsed by `commands.parse_command` and handled
  immediately, never queued. They are slash-prefixed (`/stop`, `/clear`,
  `/verbose`, `/debug`, `/model`, `/cd <name>`, …); the permission replies
  (`ok`/`no`) are the only bare keywords. `/compact`, `/context` and `/usage`
  are the exception — they are forwarded verbatim to Claude as
  SDK-dispatchable slash commands rather than handled locally. An unrecognized
  `/...` becomes `UNKNOWN`, not a prompt.
- Voice and image messages (`handle_audio`, `handle_image`) skip command
  parsing entirely and always run as prompts — voice can't reliably dictate
  slash commands, and an image carries no command intent.
- Claude's text replies are always streamed block by block as they arrive;
  `_verbose` additionally surfaces tool calls and subagent progress events.
  A heuristic (`_looks_like_narration`) drops text blocks ending in `:`/`：`
  in non-verbose mode — they are almost always pre-tool intent narration
  ("now I'll do X:"). The terminal `ResultMessage.result` is a safety net
  that fires only when the whole turn was filtered out.
- `TodoWrite` is special-cased: it produces a `TodoEvent` (not a `ToolEvent`)
  and the orchestrator pushes a phone-friendly checklist (`render_todos`)
  **in brief mode too**, deduped against the previous snapshot so repeated
  identical lists stay quiet.
- `_dry_run` (debug mode, set by `/debug on|off`) makes `_run` echo the
  prompt back instead of running a Claude turn — used to verify the DingTalk
  round-trip without spending a turn.
- Permission escalations are serialized by `_permission_lock` so parallel tool
  calls each get their own prompt-and-wait rather than racing a shared future.

### Background subagents

A turn can spawn detached subagents (`TaskStartedMessage`) that finish *after*
the main `ResultMessage`. `ClaudeRunner._drain_background` keeps the SDK
client connected past the turn so the completion (`TaskNotificationMessage`)
still has a live receiver — the per-turn `client.disconnect()` would
otherwise drop it. Two timeouts gate the wait: `_SETTLE_TIMEOUT` (15s, quiet
period after every pending subagent has been SDK-acknowledged) and
`_STUCK_TIMEOUT` (180s, hard cap for a truly stuck subagent → emits a
`timeout` TaskEvent only for un-acknowledged ids).

On the orchestrator side, `_arm_pending_notice` runs a `_PENDING_NOTICE_DELAY`
(30s) timer that messages the phone when un-acknowledged background agents
outlive a turn. Acknowledged-but-not-notified subagents are subtracted so we
never warn about agents the SDK already considers done.

### Geo gate

When `config.yaml` has a `geo` section, `build_orchestrator` wires a `geo_check`
callable into the orchestrator and a `proxy_url` into `ClaudeRunner`. `_run`
checks the exit IP's country (`geo.check_geo`, via the local proxy) before each
turn; a non-matching country skips the turn and notifies the phone. The geo
check runs *before* the `_dry_run` short-circuit, so debug mode still exercises
it. The proxy applies only to the geo request and the Claude subprocess
(`ClaudeAgentOptions.env`) — never to `os.environ`, so the DingTalk REST push
stays direct. Omitting the `geo` section disables all of this.

### Permission flow

`ClaudeRunner` passes every tool call through `can_use_tool` →
`orchestrator.request_permission` → `PermissionPolicy.evaluate`
(`permissions.py`). The policy auto-allows read-only tools, edits inside the
current project directory, and whitelisted Bash command prefixes. Bash commands
containing shell metacharacters (`&&`, `|`, `;`, `$()`, …) always escalate.
Anything not allowed waits on a future that the phone resolves with `ok`/`no`,
or times out (`permission_timeout_seconds`) into a deny.

`AskUserQuestion` is special-cased: instead of an allow/deny prompt, the runner
calls `orchestrator.answer_question`, which pushes the question and numbered
options to the phone, waits for a reply, and returns the answer to Claude as the
`PermissionResultDeny` message (`interrupt=False`, so the turn continues).

### Sessions

`ClaudeRunner` keeps a Claude session ID per project path and passes it as
`resume` on the next turn, so each project has a continuous conversation.
`reset` (on project switch or the `/clear` command) drops the session ID.

The phone can inspect and switch sessions: `/session` shows the current
session id; `/resume` lists recent sessions (including ones produced by the
desktop TUI) and `/resume <n>` / `/resume <id>` switches to one, enabling
cross-device handoff. The switch is sticky until `/clear`, `/cd`, or another
`/resume`.

### Daemon packaging (`launchd.py`)

macOS 26 forbids regular processes from writing `~/Library/LaunchAgents`, so
`make daemon-install` builds an `~/Applications/Claude DingTalk Bridge.app`
bundle (Info.plist, ad-hoc-signed) with the agent plist nested at
`Contents/Library/LaunchAgents/` and a small Swift helper
(`resources/AppHelper.swift`, compiled at install time via `xcrun swiftc`)
that registers the service through SMAppService and execs the daemon when
launchd starts it. Install failures usually trace to a missing `xcode-select`
tooling chain. `make daemon-start/stop/restart` go through `launchctl
kickstart`/`bootstrap`/`bootout` against `gui/<uid>/<label>` — never touch
`~/Library/LaunchAgents` by hand.

`cli.py._notify_phone` pushes a phone notice on `start`/`stop`/`restart`.
The daemon itself can't tell stop from restart (both arrive as SIGTERM), so
these labels live at the CLI boundary where the user's intent is unambiguous.

### Logging

`daemon.run` installs a formatter that stamps every in-turn line with
`session=<8-char-prefix> turn=<n>` via `log_context` contextvars, so a single
`grep session=…` slices one session's full trace out of multi-project log
streams. Tool calls in SDK message summaries render as `<name>#<8-char>`
(`_short_tool_id`) so a request and its result pair by eye. INFO/DEBUG go to
stdout, WARNING+ to stderr — `make logs-tail`/`logs-web` show them together.

### Prompt caching

`ClaudeRunner._build_options` keeps the cached system-prompt prefix byte-stable
across turns: it requests the `claude_code` preset with
`exclude_dynamic_sections` (the dynamic git-status block would otherwise change
the prefix every turn) and sets `ENABLE_PROMPT_CACHING_1H`, since phone turns
are minutes apart and the default 5-minute cache window is almost always cold.
`record_usage` folds each turn's token usage into a per-project tally;
`/status` surfaces the running total and the last turn's cache read/write
breakdown.

## Conventions

- Config is loaded from `~/.config/claude-dingtalk-bridge/config.yaml` (see
  `config.example.yaml`); `config.py` parses it into frozen-ish dataclasses and
  raises `ConfigError` on anything missing.
- Everything is in English — user-facing strings sent to the phone, code,
  logs, and comments. Phone messages lead with one emoji icon and use short
  bulleted lines so they read well on a phone.
- The daemon must never let one bad message kill the loop — handlers catch
  broadly and log.
- Reuse the formatting helpers in `sessions.py` instead of inlining new
  versions:
  - `format_tokens(n)` for token counts (`1.2K` / `45K` / `1.5M`).
  - Anywhere a filesystem path is rendered to the phone or log, apply the
    two-step shortening rule (project-relative first, then `$HOME` → `~`).
    Pick the helper by **input shape**, not by which one looks shorter:
    - `display_path(path)` when the input is a single whole filesystem
      path. Uses path-boundary-aware `startswith(cwd + "/")` matching, so
      a same-prefix-but-not-inside path like `/projstuff/x` (with cwd
      `/proj`) is left alone.
    - `collapse_inline_paths(s)` when the input is free-form text that
      *contains* paths (Bash commands, tool previews, log lines). Uses
      lenient substring substitution — fast and good for typical command
      text, but **don't pass a single path to it**; use `display_path` for
      that. `tool_summary()` already wraps its output in this, so phone
      ToolEvents and the permission prompt inherit it — don't apply
      collapse a second time.
    Both helpers read the active turn's project root from
    `log_context.cwd_label()`; tests or callers outside the turn loop pass
    `cwd=` explicitly.
