# CLAUDE.md

## What this is

A macOS daemon that drives Claude Code on your computer from your phone via
DingTalk. It connects in DingTalk Stream mode (outbound-only) — no public IP or
tunnel. The phone sends prompts and control commands; the daemon runs Claude
Code turns and escalates risky operations back to the phone for approval.

## Commands

`make` (no args) lists everything. Key ones:

- `make setup` — create `.venv`, `pip install -e ".[dev]"`
- `make test` — `pytest -q`
- `make daemon-*` — launchd lifecycle (install/uninstall/start/stop/restart/status)
- `make logs-tail` — tail daemon logs; `make logs-web ARGS=...` — browser live-viewer
  (`scripts/log_server.py`, date-range filtering via `--since`/`--until`)

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

The bridge's config carries only a `deny` list — there is no user-configurable
allow list. Edit-shaped tools (`Edit`/`Write`/`MultiEdit`/`NotebookEdit`) are
auto-allowed inside the active project root (an edit whose target *resolves*
outside the root — or inside `.git`/`.claude` — still escalates, see the
edit-path hook below); everything else falls through to the phone prompt. Three layers gate each tool call, in
SDK pipeline order:

1. **PreToolUse hooks** — run before settings-layer resolution, so a decision
   here cannot be short-circuited by an allow rule in any settings layer. Both
   are **one-sided: they only ever escalate to `ask` (phone approval), never
   return a hard deny.** Two hooks are installed:

   - **Bash hook** (`permission_hooks.decide_bash`, always on for Bash calls),
     in order:
     - **Tripwires** (fire even when `deny` is empty): `rm -f`/`-rf` (force in
       any short-flag group, bundled or split like `rm -r -f`), `rm --force`,
       `dd of=/dev/*`, `mkfs`, redirect to a raw block device, fork bomb.
       Matched twice — raw string (backstop, also covers parse failure + the
       structural fork bomb) then the `bashlex`-parsed command
       (`_parsed_tripwire`), which basename-normalizes the program and rebuilds
       the atom from unquoted words, so `"rm" -rf` / `/bin/rm` / `mk''fs` /
       `of="/dev/sda"` no longer slip past. Still evaded by a runtime expansion
       hiding a flag (`$IFS`, `$'\x2d\x72\x66'`), encoding, or interpreter
       wrapper; plain `rm -r` (no force) is *not* caught (use a deny rule).
     - **Unconditional escalations**: a command whose *name* is a variable
       expansion (`$CMD`, `${CMD}`, incl. inside `sh -c`) hides the program from
       tripwires and deny matcher; a write into `.git`/`.claude`
       (`_protected_write_reason`) via a `>`/`>>` redirect target, a file-writer
       operand (`cp`/`mv`/`install`/`tee` positionals, `dd of=`), or any of those
       reached by a relative path after a `cd` into the subtree (cd-targets join
       the resolution bases). A variable as an *argument* (`rm $FILE`) is left to
       lower layers; a write path built from a runtime expansion, or hidden in an
       interpreter/encoding wrapper, is still out of scope.
     - **Deny matching**: parses with `bashlex`, splits pipelines/chains into
       atoms, strips transparent wrappers (`exec`/`command`/`env`/`nice`/
       `timeout`/`sudo`/`xargs`/…) and recurses into both `sh -c "…"` (any `-c`
       group, e.g. `-cl`) and `eval "…"`, then checks each atom against
       `rules.deny` — so `Bash(rm:*)` catches `cd /tmp && bash -c "rm -rf /foo"`. A
       path-prefixed program is matched on its basename too (`Bash(rm:*)` ⇒
       `/bin/rm`, `./rm`), while an explicit `Bash(/bin/rm:*)` still matches the
       path form.
     - **Degrade to `ask`** (when deny rules are set): unmodeled wrapper flags
       (`exec -a`, `command -p`, `env -i`/`-u`, `sudo -u`, `xargs -0`) leave the
       atom opaque; unparseable input; opaque constructs (subshell, process/
       command substitution, file redirect, heredoc, compound, `if`/`for`/
       `while`/`until`/`case`/`function`). Benign redirects (fd dups,
       `/dev/null`) are exempt. Anything else returns `{}` and falls through.
   - **Edit-path hook** (`permission_hooks.decide_edit`, on for the four
     edit-shaped tools). The settings layer auto-allows `Edit(<cwd>/**)` by
     *textual* glob, which a `..` segment or an in-tree symlink can slip past;
     this hook resolves the target and the project root and escalates to `ask`
     when the resolved target lands outside the root (a resolution failure, e.g.
     a symlink loop, also escalates), or inside a protected subdir
     (`.git`/`.claude`) where an in-tree edit could plant a git hook or inject
     allow rules. Other in-project targets return `{}` and let the settings
     allow auto-approve them.

   Anything a hook leaves at `{}` falls through to layer 2.
2. **Claude Agent SDK settings rules** — `permissions.write_permission_settings_file`
   serializes the bridge's deny list plus the in-project edit expansion
   (`Edit(<cwd>/**)`, …) into a JSON file passed as
   `ClaudeAgentOptions(settings=...)`. The SDK applies these before any Python
   runs, and merges them with lower settings layers; the bridge's flag-layer
   deny wins on conflict. A deny resolved here is a **hard block the CLI
   enforces directly** — `can_use_tool` is never invoked and the model just
   receives the tool denial; this is the only layer that hard-denies a tool.
3. **`can_use_tool` callback** (`ClaudeRunner`) — fires only for calls the
   first two layers didn't decide (including a layer-1 `ask`). It has no rules
   of its own; the call goes straight to `permission_handler` (the phone ask).
   `AskUserQuestion` is intercepted separately and routed to `question_handler`.

Phone escalations and questions are serialized by `_permission_lock`, so
parallel tool calls each get their own prompt-and-wait instead of racing a
shared future. An escalation default-denies after `permission_timeout_seconds`
(config, 600s); tripwire-matched Bash shows a louder `‼️` icon vs the routine
`🔐`. `scripts/check_bash_permissions.py` is a standalone harness for exercising
`decide_bash` by hand.

`/mode` overrides the SDK `permission_mode` for subsequent turns
(`acceptEdits`/`bypassPermissions`/`plan`/`dontAsk`/`default`/`auto`, or `reset`
to fall back to the TUI's settings). `bypassPermissions` skips the layer-3 phone
ask entirely, so it disables the approval model — use with care.

### Geo gate

With a `geo` section in `config.yaml`, `build_orchestrator` wires a `geo_check`
callable into the orchestrator and `proxy_url` into `ClaudeRunner`. `_run` checks
the exit IP's country (`geo.check_geo`, via the local proxy) before each turn and
skips + notifies on a mismatch. The check runs *before* the debug short-circuit,
so `/debug on` still exercises it. The proxy applies only to the geo request and
the Claude subprocess (`ClaudeAgentOptions.env`) — never `os.environ`, so the
DingTalk REST push stays direct. No `geo` section → all of this is off.

### Sessions

The orchestrator holds a `projects.ProjectRegistry` of the configured projects
and an active `_current_project` (defaults to the first in config); `/cd <name>`
switches it. Each project path keeps its own session, so switching is a context
swap, not a reset.

`ClaudeRunner` keeps a Claude session ID per project path and passes it as
`resume` next turn, giving each project a continuous conversation. Sticky until
`/clear`, `/cd`, or `/resume` drops or replaces it — `/resume` can adopt a
session produced by the desktop TUI, enabling cross-device handoff.

### Stream reconnect

`stream_reconnect.ReconnectState` is a backoff machine for the DingTalk Stream
WebSocket (`daemon` reconnect loop). The gateway penalizes rapid reconnects with
prolonged lockouts (~30 min observed) and doesn't queue inbound messages while
the bot is offline, so the SDK's flat 10s retry can turn a blip into a long
outage. Delays climb `10→30→90→300s` with jitter; a connection that stayed up
≥`stable_threshold` (60s) resets the count.

### Daemon packaging (`launchd.py`)

macOS 26 forbids regular processes from writing `~/Library/LaunchAgents`, so
`make daemon-install` builds an `~/Applications/Claude DingTalk Bridge.app`
bundle (Info.plist, ad-hoc-signed) with the agent plist nested at
`Contents/Library/LaunchAgents/` and a Swift helper (`resources/AppHelper.swift`,
compiled at install via `xcrun swiftc`) that registers the service through
SMAppService and execs the daemon. Install failures usually trace to a missing
`xcode-select` toolchain. `make daemon-start/stop/restart` go through `launchctl
kickstart`/`bootstrap`/`bootout` on `gui/<uid>/<label>` — never touch
`~/Library/LaunchAgents` by hand.

`cli.py._notify_phone` pushes a notice on `start`/`stop`/`restart`. The daemon
can't tell stop from restart (both arrive as SIGTERM), so these labels live at
the CLI boundary where intent is unambiguous.

### Logging

`daemon.run` installs a formatter that stamps each in-turn line with
`session=<8-char> turn=<n>` (`log_context` contextvars), so `grep session=…`
slices one session's full trace out of multi-project streams. Tool calls render
as `<name>#<8-char id>` (via `_short_tool_id`) so a request and its result pair
by eye. INFO/DEBUG → stdout, WARNING+ → stderr; `make logs-tail` shows both, `make
logs-web` serves a browser live-viewer (`scripts/log_server.py`, date-range
filtering).

### Prompt caching

`ClaudeRunner._build_options` keeps the cached system-prompt prefix byte-stable:
it requests the `claude_code` preset with `exclude_dynamic_sections` (the dynamic
git-status block would otherwise change the prefix every turn) and sets
`ENABLE_PROMPT_CACHING_1H`, since phone turns are minutes apart and the default
5-minute window is almost always cold. `record_usage` tallies token usage per
project; `/status` shows the running total and the last turn's cache read/write
breakdown.

## Conventions

- Config: `~/.config/claude-dingtalk-bridge/config.yaml` (see
  `config.example.yaml`); `config.py` parses it into frozen-ish dataclasses,
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
