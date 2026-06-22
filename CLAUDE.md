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
- `make test` — combined `pytest -q --cov` run, then a per-module isolated
  branch-coverage gate (`scripts/coverage_isolated.py`): each `tests/test_<mod>.py`
  re-run alone against only its own module, so incidental cross-module coverage
  can't mask a weak per-file test. Anything below 100% fails the build.
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
  DingTalk Open API REST endpoints, managing its own access token. Sync/`requests`-
  based, called from async via `asyncio.to_thread`. Replies over the per-message
  limit are split by `chunking.chunk_markdown` — a **UTF-8 byte**-budgeted,
  fence-aware splitter that reopens/closes a carried-over code fence across
  boundaries — each piece run through `pad_code_tail`; `daemon.send_markdown` lifts
  the title per chunk.

`daemon.build_orchestrator` wires everything. The `ClaudeRunner` ↔ orchestrator
cycle is broken by late assignment: after both exist, the runner's
`permission_handler` and `question_handler` are set to the orchestrator's
`request_permission` and `answer_question`.

### Orchestrator (`orchestrator.py`) — the core

Single-threaded async coordinator holding all mutable session state.

- One task at a time (`self._task`); prompts arriving mid-task are queued
  (`self._queue`) and drained sequentially. `/queue` views/edits it (`/queue rm
  N`, `/queue clear`). A bare `/stop` interrupts the turn and lets the next queued
  prompt auto-start; `/stop all` clears the queue **before** the abort so `_run`'s
  finally can't auto-advance. Exception: a prompt arriving during the runner's
  post-turn drain (`runner.is_draining`) calls `cancel_drain()` and starts
  immediately, rather than waiting out a possibly-stuck background-agent wait.
- Control commands (`commands.parse_command`, full map in `commands.py`) are
  handled immediately, never queued: slash-prefixed (`/stop`, `/cd`, `/resume`,
  `/update`, …) plus bare permission replies (`ok`/`yes`/👌, `no`/`deny`/❌).
  `/compact`, `/context` are forwarded verbatim as SDK slash commands. An
  unrecognized `/...` becomes `UNKNOWN`, not a prompt.
- Command help lives in one place: `commands.HELP` (a `{name: HelpEntry}`
  registry). `/help` renders the grouped one-line list; `/help <command>` adds
  that entry's `detail`. Inline "usage" errors (e.g. `/resume`, `/queue rm …`)
  pull from the same registry so help and errors never drift. `_cmd_help`
  groups by `HELP_GROUPS` order.
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
  (`make_bash_permission_hook`) blocking a fixed set of catastrophic literals
  (`rm -rf`, `find -delete`, `dd of=/dev/…`, `diskutil`/`asr`/`gpt` verbs, fork
  bomb, …) and variable-substituted command names. Parses with `bashlex`, recurses
  into `bash -c`/`eval`. Returns `deny` — the only verdict that holds across every
  `permission_mode`, so no allow-rule or `bypassPermissions` can undo it.
  Path-level rules (`Bash(rm:*)`) live in Claude Code's own settings, not here.
  `make check` smoke-tests it (`scripts/check_bash_permissions.py`).

### Geo gate (optional)

If config carries a `geo:` block, each turn first checks the exit IP's country
through the configured proxy (`geo.py`, result cached briefly). A mismatch with
`target_country` appends a warning to the task-started notice but does **not**
block the turn. `geo.proxy_url` is also pushed into Claude's subprocess env
(`http_proxy`/`https_proxy`) so its traffic shares the proxy. Omit the section to
skip both the check and the proxy.

### Self-update (`self_update.py`)

`/update` updates the daemon's **own** repo (not the user's `projects`).
`self_update.py` wraps the git/make steps as async helpers (`pull` is `--ff-only`
only), raising `SelfUpdateError` (with captured output) on failure. `_cmd_update`
refuses while a turn runs (like `/cd`), reports "up to date" when not behind, else
pulls, runs `make setup`/`config` only when `pyproject.toml`/`config.example.yaml`
changed, then **confirms before restarting** via `self._restart_confirm` (bare
`ok`/`no` resolves it, ahead of the tool-permission path). Restart runs detached
(`start_new_session=True`) so `launchctl kickstart -k` survives the daemon's own
SIGTERM; the new instance sends the "🔄 Daemon restarted" notice.

`daemon._auto_update_loop` checks 60s after startup then every 24h: a silent
`fetch_and_compare` that nudges the phone only when behind. Up-to-date and errors
stay silent (errors logged) so a failed check (offline, or SSH-auth under
launchd's minimal env) can't become daily noise. Wired in `_serve`.

### Sessions

The orchestrator holds a `projects.ProjectRegistry` of the configured projects
and an active `_current_project` (defaults to the first in config); `/cd <name>`
switches it. `/cd` resets the target project's session (and its usage tallies)
via `runner.reset`, so a switch starts that project fresh.

`/ls reload` picks up a hand-edited `projects` list without restarting:
`config.load_projects` re-reads **only** that section and rebuilds the registry;
the rest of the config and all session state stay live. If the active project's
name vanished, the reload falls back to the default (resetting that session and
announcing the switch); an edited path on a still-present name just re-points
`_current_project` at the fresh object.

`ClaudeRunner` keeps a Claude session ID per project path and passes it as
`resume` next turn, giving each project a continuous conversation. Sticky until
`/clear`, `/cd`, or `/resume` drops or replaces it — `/resume` can adopt a
session produced by the desktop TUI, enabling cross-device handoff.

### Stream reconnect

`stream_reconnect.ReconnectState` is the backoff machine for the DingTalk Stream
WebSocket (`daemon` reconnect loop). The gateway locks out rapid reconnects
(~30 min observed) and drops inbound messages while offline, so a flat retry can
stretch a blip into a long outage. Delays climb `10→30→90→300s` with jitter; a
connection up ≥`stable_threshold` (60s) resets the count. Liveness and outage
duration use **wall-clock** time (`time.time()`), not `monotonic` — monotonic
freezes during macOS sleep, which would mis-measure an overnight connection.

Two watchers (`connectivity.py`) break an obsolete backoff via a shared
`retry_now` event, but **only while down** (gated on the `disconnected` event; a
live socket's own I/O surfaces any death). The loop classifies the wake once via
`wake_is_dark`, reading the **live** power state from `pmset -g systemstate`
(`parse_capabilities_are_dark`): the `Graphics` capability present → full wake,
reconnect now; a maintenance **DarkWake** (lid shut, every few minutes) lacks it →
stay in backoff so the daemon doesn't flap all night. Fails open (no capabilities
line → awake). Reading *current* capabilities, not scraping the async-written
`pmset -g log`, is deliberate: the log lags the wake (a 2s DarkWake once
reconnected before its row was published), while the capability flips with the
wake — no race, no recency window. Display *idle*-sleep keeps `Graphics`, so an
awake screen-off outage isn't misread as dark. `watch_wake` covers a wake *during* backoff (clock-skew baseline,
kept fresh even when connected); `watch_reachability` edge-triggers on the network
returning via a **zero-traffic** UDP `connect()` (route lookup, no packet, can't
trip the lockout). Both freeze during sleep, costing nothing until wake.

The "Reconnected after ~Xm offline" notice must report the **true** outage
including pre-drop suspend, which a frozen socket can't see (its keepalive only
fails *after* wake). `_drive_stream_client` measures the suspend by clock skew and
reports `slept + gap` (see `daemon.py` for the math). A `slept` over
`WAKE_SKEW_THRESHOLD` also self-nudges `retry_now` for an immediate reconnect;
`_on_connect` clears `retry_now` so a mid-connect signal can't skip the next
backoff. `_serve` spawns both watchers; shutdown **awaits** any in-flight offline
notice up to `_SHUTDOWN_NOTICE_TIMEOUT` so the resend hint isn't dropped.

### Daemon packaging (`launchd.py`)

macOS 26 forbids regular processes from writing `~/Library/LaunchAgents`, so
`make daemon-install` builds an ad-hoc-signed `~/Applications/Claude DingTalk
Bridge.app` bundle with the agent plist nested at `Contents/Library/LaunchAgents/`;
a Swift helper (`resources/AppHelper.swift`, compiled via `xcrun swiftc`) registers
it through SMAppService and execs the daemon. Install failures usually mean a
missing `xcode-select` toolchain. `daemon-start` runs `launchctl kickstart`
(falling back to `bootstrap` when booted out), `stop` runs `bootout`, `restart`
runs `kickstart -k` — never touch `~/Library/LaunchAgents` by hand.

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
- Reuse formatting helpers, don't inline: `display.format_tokens(n)` for token
  counts; `display.display_path(path)` for a whole path, or
  `display.collapse_inline_paths(s)` for free-form text *containing* paths.
  `tool_summary()` (`claude_runner.py`) already collapses — don't double-collapse
  phone ToolEvents or the permission prompt. Both read the turn root from
  `log_context.cwd_label()`; callers outside the turn loop pass `cwd=` explicitly.
- Phone rendering — DingTalk renders `sampleMarkdown` in a smaller font than chat
  bubbles, so default short notices to `self._send` (sampleText) and reserve
  `self._send_markdown` for content that needs formatting (Claude's reply,
  `render_todos`, `_cmd_status`, the permission prompt). Don't auto-detect
  markdown metacharacters — `_`, `#`, `*`, backticks all appear in routine
  identifiers and cause too many false positives.
- `daemon.send_markdown` lifts the body's first `#{1,6}` heading as the DingTalk
  notification title (heading stays in the body); no heading → `"Claude has
  replied."`. Don't hardcode a separate title.
