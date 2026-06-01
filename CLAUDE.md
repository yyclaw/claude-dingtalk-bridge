# CLAUDE.md

## What this is

A macOS daemon that drives Claude Code on your computer from your phone via
DingTalk. It connects in DingTalk Stream mode (outbound-only) — no public IP or
tunnel. The phone sends prompts and control commands; the daemon runs Claude
Code turns and escalates risky operations back to the phone for approval.

## Commands

`make` (no args) lists everything. Key ones:

- `make setup` — create `.venv`, `pip install -e ".[dev]"`
- `make config` — write `config.yaml` from the template if absent
- `make test` — `pytest -q --cov` (with a branch-coverage summary)
- `make start` — run the daemon in the foreground (logs to terminal)
- `make daemon-*` — launchd lifecycle (install/uninstall/start/stop/restart/status)
- `make logs-tail` — tail daemon logs; `make logs-web ARGS=...` — browser live-viewer
  (`scripts/log_server.py`, date-range filtering via `--since`/`--until`)
- `make check` — smoke-test the Bash permission hook against a table of commands

Single test: `.venv/bin/pytest tests/test_orchestrator.py -q` (append
`::test_name`). `pytest-asyncio` runs in `auto` mode — async tests need no
decorator.

## Architecture

Inbound and outbound messages travel separate paths:

- **Inbound**: `dingtalk_stream` opens a persistent WebSocket; `daemon._ChatHandler`
  dispatches by type. `text` → orchestrator; `audio` → DingTalk's own
  transcription (read from the message extensions; the audio is never fetched);
  `picture`/`richText` → images downloaded (`images.download_image`) and
  assembled into a prompt (`build_image_prompt`) before the turn runs.
- **Outbound**: `DingTalkTransport` (`dingtalk.py`) pushes 1:1 messages via the
  DingTalk Open API REST endpoints, managing its own access token. It is
  sync/`requests`-based, called from async code via `asyncio.to_thread`.

`daemon.build_orchestrator` wires everything. The `ClaudeRunner` ↔ orchestrator
cycle is broken by late assignment: after both exist, the runner's
`permission_handler` and `question_handler` are set to the orchestrator's
`request_permission` and `answer_question`.

### Orchestrator (`orchestrator.py`) — the core

Single-threaded async coordinator holding all mutable session state.

- One task at a time (`self._task`); prompts arriving mid-task are queued
  (`self._queue`) and drained sequentially. Exception: a prompt arriving while
  the runner is in its post-turn drain (`runner.is_draining`) calls
  `cancel_drain()` and starts immediately, rather than waiting out a possibly-
  stuck background-agent wait.
- Control commands (`commands.parse_command`) are handled immediately, never
  queued: slash-prefixed (`/stop`, `/clear`, `/verbose`, `/debug`, `/model`,
  `/mode`, `/cd`, `/ls`, `/pwd`, `/status`, `/session`, `/resume`, `/help`; full
  map in `commands.py`) plus the bare permission replies (`ok`/`yes`/`approve`/👌,
  `no`/`deny`/`reject`/❌). `/compact`, `/context`, `/usage` are forwarded verbatim
  to Claude as SDK slash commands. An unrecognized `/...` becomes `UNKNOWN`, not a
  prompt.
- `handle_audio` and `handle_image` skip command parsing and always run as
  prompts — voice can't reliably dictate slash commands, and an image carries no
  command intent.
- Text replies stream block by block; `_verbose` additionally surfaces tool
  calls and subagent progress. The terminal `ResultMessage.result` is a safety
  net that fires only when the whole turn was filtered out.
- `TodoWrite` yields a `TodoEvent` (not a `ToolEvent`); the checklist
  (`render_todos`) is pushed **even in brief mode**, deduped against the previous
  snapshot so repeated identical lists stay quiet.

### Permissions

Two independent layers gate tool use:

- **Interactive approval** — the SDK's `can_use_tool` callback routes risky
  operations to `orchestrator.request_permission`, which asks the phone
  (`ok`/`no` resolves it); `AskUserQuestion` routes to `answer_question` the
  same way.
- **Hard-deny hook** (`permission_hooks.py`) — a `PreToolUse` Bash hook
  (`make_bash_permission_hook`) that blocks a fixed set of catastrophic literals
  (`rm -rf` incl. split flags, `find -delete`, `dd of=/dev/…`, `newfs`,
  `diskutil`/`asr`/`gpt` destructive verbs, fork bomb, redirect to a block
  device) and variable-substituted command names (`$CMD`). It parses with `bashlex` and
  recurses into `bash -c`/`eval`. It returns `deny` — the only verdict that holds
  across every `permission_mode`, so no settings allow-rule or `bypassPermissions`
  can undo it. Path-level rules (`Bash(rm:*)`) are *not* here — those live in
  Claude Code's own settings layers. `make check` smoke-tests the guard
  (`scripts/check_bash_permissions.py`).

### Geo gate (optional)

If config carries a `geo:` block, each turn first checks the exit IP's country
through the configured proxy (`geo.py`, result cached briefly). A mismatch with
`target_country` appends a warning to the task-started notice but does **not**
block the turn. `geo.proxy_url` is also pushed into Claude's subprocess env
(`http_proxy`/`https_proxy`) so its traffic shares the proxy. Omit the section to
skip both the check and the proxy.

### Sessions

The orchestrator holds a `projects.ProjectRegistry` of the configured projects
and an active `_current_project` (defaults to the first in config); `/cd <name>`
switches it. `/cd` resets the target project's session (and its usage tallies)
via `runner.reset`, so a switch starts that project fresh.

`ClaudeRunner` keeps a Claude session ID per project path and passes it as
`resume` next turn, giving each project a continuous conversation. Sticky until
`/clear`, `/cd`, or `/resume` drops or replaces it — `/resume` can adopt a
session produced by the desktop TUI, enabling cross-device handoff.

### Stream reconnect

`stream_reconnect.ReconnectState` is a backoff machine for the DingTalk Stream
WebSocket (`daemon` reconnect loop). The gateway locks out rapid reconnects
(~30 min observed) and drops inbound messages while offline, so the SDK's flat
10s retry can stretch a blip into a long outage. Delays climb `10→30→90→300s`
with jitter; a connection up ≥`stable_threshold` (60s) resets the count.

### Daemon packaging (`launchd.py`)

macOS 26 forbids regular processes from writing `~/Library/LaunchAgents`, so
`make daemon-install` builds an ad-hoc-signed `~/Applications/Claude DingTalk
Bridge.app` bundle (Info.plist) with the agent plist nested at
`Contents/Library/LaunchAgents/`; a Swift helper (`resources/AppHelper.swift`,
compiled at install via `xcrun swiftc`) registers the service through
SMAppService and execs the daemon. Install failures usually mean a missing
`xcode-select` toolchain. `daemon-start` runs `launchctl kickstart
gui/<uid>/<label>` (falling back to `bootstrap gui/<uid> <plist>` when booted
out), `stop` runs `bootout`, `restart` runs `kickstart -k` — never touch
`~/Library/LaunchAgents` by hand.

`cli.py._notify_phone` pushes a notice on `start`/`stop`/`restart`. The daemon
can't tell stop from restart (both arrive as SIGTERM), so these labels live at
the CLI boundary where intent is unambiguous.

### Logging

`daemon.run` installs a formatter that stamps each in-turn line with
`session=<8-char> turn=<n>` (`log_context` contextvars), so `grep session=…`
slices one session's full trace out of multi-project streams. Tool calls render
as `<name>#<8-char id>` (via `_short_tool_id`) so a request and its result pair
by eye. INFO/DEBUG → stdout, WARNING+ → stderr; `make logs-tail` shows both,
`make logs-web` serves the live-viewer.

### Prompt caching

`ClaudeRunner._build_options` keeps the cached system-prompt prefix byte-stable:
the `claude_code` preset with `exclude_dynamic_sections` (the dynamic git-status
block would otherwise change the prefix every turn), plus `ENABLE_PROMPT_CACHING_1H`
because phone turns are minutes apart and the default 5-minute window is usually
cold. `record_usage` tallies tokens per project; `/status` shows the running
total and the last turn's cache read/write breakdown.

## Conventions

- Config: `~/.config/claude-dingtalk-bridge/config.yaml` (see
  `config.example.yaml`); `config.py` parses it into dataclasses,
  raising `ConfigError` on anything missing.
- Everything is in English — phone strings, code, logs, comments. Phone messages
  lead with one emoji icon and use short bulleted lines.
- The daemon must never let one bad message kill the loop — handlers catch
  broadly and log.
- Reuse the formatting helpers instead of inlining new ones:
  - `display.format_tokens(n)` for token counts (`1.2K` / `45K` / `1.5M`).
  - For any path rendered to phone or log, apply the two-step shortening
    (project-relative first, then `$HOME` → `~`). Pick by **input shape**:
    - `display.display_path(path)` for a single whole path.
    - `display.collapse_inline_paths(s)` for free-form text *containing* paths
      (Bash commands, tool previews, log lines). `tool_summary()`
      (`claude_runner.py`) already wraps its output in this — don't collapse a
      second time on phone ToolEvents or the permission prompt.
    Both read the active turn's project root from `log_context.cwd_label()`;
    callers outside the turn loop pass `cwd=` explicitly.
- Phone rendering — DingTalk renders `sampleMarkdown` in a smaller font than chat
  bubbles, so default short notices to `self._send` (sampleText) and reserve
  `self._send_markdown` for content that needs formatting (Claude's reply,
  `render_todos`, `_cmd_status`, the permission prompt). Don't auto-detect
  markdown metacharacters — `_`, `#`, `*`, backticks all appear in routine
  identifiers and cause too many false positives.
- `daemon.send_markdown` lifts the body's first `#{1,6}` heading as the DingTalk
  notification title (heading stays in the body); no heading → `"Claude has
  replied."`. Don't hardcode a separate title.
