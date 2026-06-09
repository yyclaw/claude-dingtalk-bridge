from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
from typing import Awaitable, Callable

from claude_dingtalk_bridge.claude_runner import (
    aggregate_model_usage,
    ResultEvent,
    TaskEvent,
    TextEvent,
    TodoEvent,
    ToolEvent,
    _cache_breakdown,
    _model_cache_breakdown,
    tool_summary,
)
from claude_dingtalk_bridge.commands import (
    HELP,
    HELP_GROUPS,
    CommandType,
    parse_command,
)
from pathlib import Path

from claude_dingtalk_bridge.config import (
    Config,
    ConfigError,
    DEFAULT_CONFIG_PATH,
    load_projects,
)
from claude_dingtalk_bridge.geo import GeoCheck
from claude_dingtalk_bridge import log_context, self_update
from claude_dingtalk_bridge.projects import ProjectRegistry
from claude_dingtalk_bridge.questions import (
    format_question,
    parse_answer,
    question_label,
    question_preview,
)
from claude_dingtalk_bridge.display import (
    MD_SPACER,
    collapse_inline_paths,
    display_path,
    format_cost,
    format_tokens,
    md_escape,
    short_model_name,
)
from claude_dingtalk_bridge.sessions import (
    find_session,
    format_session_list,
    is_uuid,
    list_recent_sessions,
    session_transcript_path,
)

logger = logging.getLogger(__name__)

Send = Callable[[str], Awaitable[None]]

_PROMPT_SUMMARY_LIMIT = 80
_RESUME_LIST_LIMIT = 7
# Phone turns are minutes apart and subagents often finish within seconds —
# delay the "still running" notice so a quick subagent never triggers it.
# Restarted whenever pending changes; rechecked after sleep so a notification
# that arrives while the timer is about to fire still suppresses the message.
_PENDING_NOTICE_DELAY = 30.0

# A geo check goes through the proxy and can stall when the VPN is flaky.
# When it runs long, reassure the phone that we're still working on it; the
# notice is cancelled the moment the check settles (result, failure, or user
# interrupt) so a fast check stays silent. The delay is derived from the
# configured request timeout rather than fixed: a timeout at or below this floor
# is short enough to need no notice (the whole check is over too soon to bother),
# and otherwise the notice fires at 60% of the timeout — always leaving headroom
# before the request's own timeout, so a slow-but-successful check is reassured,
# not just a failing one. (A fixed delay >= the timeout could only ever fire as
# the request was already timing out.)
_GEO_SLOW_NOTICE_MIN_TIMEOUT = 5


def _geo_slow_notice_delay(timeout_seconds: int) -> float | None:
    """Seconds to wait before the 'still checking geo' notice, or None to skip
    it entirely for a timeout short enough not to warrant one."""
    if timeout_seconds <= _GEO_SLOW_NOTICE_MIN_TIMEOUT:
        return None
    return float(round(timeout_seconds * 0.6))

# Curated model aliases for the /model command. Mirrors the aliases the
# Claude CLI accepts for --model; only aliases (no version numbers) so it
# needn't change as models are bumped. A full model id can still be passed
# directly via `/model <id>` — this list is just the shortcut menu.
MODEL_NAMES: tuple[str, ...] = ("opus", "sonnet", "haiku")


# Discourse markers that open a "going-to-do-X" sentence. A text block whose
# every sentence opens with one of these reports no outcome — it's pure
# forward-looking narration ("Now the ThemeProvider.", "Let me read the
# config."), the bulk of the noise on the phone in brief mode. Kept narrow on
# purpose: pure sequencers (next/then/first/also/finally) carry no tool intent
# and routinely open genuine answer prose ("First, generate a key. Then copy
# it.") — including them dropped real replies, so they're out. Each apostrophe
# accepts the typographic variant (U+2019) the model often emits.
_FORWARD_INTENT_RE = re.compile(
    r"(?i)^(?:now|let me|let['’]s|lets|i['’]ll|i will|"
    r"i['’]m going to|i am going to)\b"
)
# Split on sentence terminators, but treat an ASCII `.!?` as a boundary only
# when whitespace or end-of-string follows — otherwise the dot in `main.tsx`
# or `index.html` falsely splits a single "Now wire X into main.tsx" sentence,
# leaving a non-forward fragment that wrongly keeps the block. CJK terminators
# and newlines always split (no intra-word dots to worry about).
_SENTENCE_SPLIT_RE = re.compile(r"[。！？\n]+|[.!?]+(?=\s|$)")


def _is_progress_filler(text: str) -> bool:
    """True when a text block carries forward-looking narration worth dropping
    in brief mode — cutting per-turn phone volume while leaving 📋 Tasks and
    subagent notices as the progress signal.

    Two drop signals, both heuristic (verbose mode bypasses them; the
    ResultMessage fallback still surfaces the final reply if a whole turn
    filtered out):

    1. Ends in a colon (``:`` / ``：``) — pre-list / pre-tool narration
       ("Here's the plan:"). Trailing markdown emphasis (``**bold:**``) is
       stripped first, else a bolded line ends in ``**`` and slips past.
    2. *Any* sentence opens with a forward-intent marker (Now / Let me /
       I'll …). Even a leading finding ("Build succeeds. Let me
       verify…", "The image shows X. Let me find Y.") drops — those
       observation+intent patterns are mostly intent dressing, not the
       milestone signal. A pure-result block with no forward tail
       ("All four changes are implemented, tested, and verified.") still
       passes through.
    """
    stripped = text.rstrip().rstrip("*_")
    if stripped.endswith((":", "：")):
        return True
    return any(
        _FORWARD_INTENT_RE.match(cleaned)
        for s in _SENTENCE_SPLIT_RE.split(stripped)
        if (cleaned := s.strip().lstrip("*_`-# ").strip())
    )


def _fmt_usage(duration_ms: int, total_tokens: int) -> str:
    """Render a subagent's completion footer like ' (3.0s, 16.6K)'.

    Returns '' when neither datum is available — the SDK leaves both at 0 on
    e.g. abnormal stops, and we'd rather drop the suffix than print '(0.0s, 0)'.
    """
    parts: list[str] = []
    if duration_ms > 0:
        parts.append(f"{duration_ms / 1000:.1f}s")
    if total_tokens > 0:
        parts.append(format_tokens(total_tokens))
    return f" ({', '.join(parts)})" if parts else ""


def _summary(text: str) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= _PROMPT_SUMMARY_LIMIT:
        return text
    return text[:_PROMPT_SUMMARY_LIMIT] + "…"


_PROMPT_LOG_SOFT = 80
_PROMPT_LOG_HARD = 300
_SENTENCE_BOUNDARIES = frozenset(".。!?！？\n")


def _prompt_log_summary(text: str) -> str:
    """Sentence-aware truncation for the `Running turn` log line.

    Fixed-length truncation (80 chars) cuts mid-word and hides the rest of
    short prompts that go just slightly over. Instead, look for a sentence
    boundary (`.`, `。`, `!`, `?`, `\\n`, …) somewhere between SOFT and HARD
    — that preserves the prompt's natural reading unit. If the first
    sentence is very short, extending naturally swallows the second.

    Falls back to hard truncation at HARD only when no boundary is found.
    """
    if len(text) <= _PROMPT_LOG_SOFT:
        return text.replace("\n", " ").strip()
    # Walk forward, take the first boundary at or past SOFT (and <= HARD).
    cut = None
    for i, ch in enumerate(text):
        if i >= _PROMPT_LOG_HARD:
            break
        if ch in _SENTENCE_BOUNDARIES and i + 1 >= _PROMPT_LOG_SOFT:
            cut = i + 1
            break
    if cut is None:
        cut = min(len(text), _PROMPT_LOG_HARD)
    out = text[:cut].replace("\n", " ").strip()
    return out + "…" if cut < len(text) else out


def render_todos(items: list[tuple[str, str, str]]) -> str:
    """Render a TodoWrite snapshot as a phone-friendly markdown checklist.

    Completed items are struck through, the in-progress one is bolded with its
    active form. A `- ` list keeps each line distinct under DingTalk's
    single-newline folding.
    """
    done = sum(1 for _, status, _ in items if status == "completed")
    lines = [f"📋 **Tasks** ({done}/{len(items)})", ""]
    for content, status, active_form in items:
        if status == "completed":
            lines.append(f"- ⚑ ~~{md_escape(content)}~~")
        elif status == "in_progress":
            lines.append(f"- ✍︎ **{md_escape(active_form or content)}**")
        else:
            lines.append(f"- ☕︎ {md_escape(content)}")
    return "\n".join(lines)


class Orchestrator:
    """Routes phone messages, runs Claude tasks, escalates permissions."""

    def __init__(
        self,
        config: Config,
        registry: ProjectRegistry,
        runner,
        send: Send,
        send_markdown: Send,
        geo_check: Callable[[], Awaitable[GeoCheck]] | None = None,
        config_path: Path | str = DEFAULT_CONFIG_PATH,
    ):
        self._config = config
        self._registry = registry
        # The config file path, re-read by /ls reload to pick up edited
        # projects. Production always loads from DEFAULT_CONFIG_PATH.
        self._config_path = config_path
        self._runner = runner
        self._send = send
        # Command replies, Claude-authored text, and the permission prompt
        # are rendered as markdown. Runtime task/progress messages keep send
        # — their line-by-line layout would collapse under DingTalk's
        # markdown newline folding.
        self._send_markdown = send_markdown
        self._verbose = False
        self._current_project = registry.default()
        self._task: asyncio.Task | None = None
        self._queue: list[str] = []
        self._permission_future: asyncio.Future[bool] | None = None
        self._permission_lock = asyncio.Lock()
        self._question_future: asyncio.Future[str | None] | None = None
        # Set while /update waits for a restart confirmation; a bare ok/no
        # resolves it (see _cmd_permission_reply). Released on any new turn,
        # /stop, /clear, or shutdown so a later ok/no can't be mistaken for it.
        self._restart_confirm: asyncio.Future[bool] | None = None
        # Guards _cmd_update against re-entry: set synchronously at entry so two
        # concurrent /update messages can't both pull/make at once.
        self._updating = False
        self._geo_check = geo_check
        self._dry_run = False
        self._resume_candidates: list[str] = []
        # Per-turn progress state, reset at the start of each _run: whether a
        # text block was sent, the last checklist string pushed (dedup), and
        # subagent task ids started but not yet notified. _turn_cancelled
        # gates _emit so a stopped/cleared turn cannot push events while it
        # unwinds. _pending_notice_task is the in-flight delayed-notice timer
        # — armed when a turn ends with subagents still running, cancelled and
        # restarted as the pending set changes. _acknowledged_tasks holds ids
        # the SDK has already marked terminal via task_updated; subtracted
        # from _pending_tasks so the "still running" notice only counts truly
        # un-finished subagents.
        self._turn_cancelled = False
        self._turn_sent_text = False
        # Gates the "say `go on` to continue" hint in /stop's reply: True only
        # after we've pushed `▶️ Task started`. A /stop landing in the geo phase
        # otherwise dangles a "continue where it left off" line at the user when
        # nothing was ever started.
        self._turn_announced = False
        self._last_todo_render: str | None = None
        self._pending_tasks: set[str] = set()
        self._acknowledged_tasks: set[str] = set()
        self._pending_notice_task: asyncio.Task | None = None

    def is_authorized(self, sender_id: str) -> bool:
        """True only for the single configured authorized DingTalk user."""
        return sender_id == self._config.authorized_user_id

    async def notify(self, message: str) -> None:
        """Push an out-of-band plain-text notice to the phone.

        For daemon-level paths (image download failures, shutdown notices)
        that need to reach the user without going through a Claude turn.
        """
        await self._send(message)

    async def shutdown(self) -> None:
        """Best-effort graceful teardown: resolve pending waits, cancel work.

        Idempotent — safe to call multiple times. Does NOT close the
        DingTalk transport: the daemon may want one last sync notification
        afterwards.
        """
        # Free anyone awaiting a phone reply so their wait_for unwinds
        # immediately rather than waiting out the timeout.
        if self._permission_future is not None and not self._permission_future.done():
            self._permission_future.set_result(False)
        if self._question_future is not None and not self._question_future.done():
            self._question_future.set_result(None)
        self._release_restart_confirm()
        # Cancel the running turn (also stops the Claude SDK child via
        # interrupt) and wait for unwind so the SDK subprocess gets a chance
        # to disconnect cleanly before the event loop dies.
        await self._abort_running_task()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._cancel_pending_notice()
        self._queue.clear()

    async def handle_message(self, text: str, sender_id: str) -> None:
        if not self.is_authorized(sender_id):
            logger.warning("Ignoring message from unauthorized sender %s", sender_id)
            return
        cmd = parse_command(text)
        # A pending question absorbs any plain-text reply — including the bare
        # ok/no keywords, which would otherwise be parsed as permission replies.
        if (
            self._question_future is not None
            and not self._question_future.done()
            and cmd.type in (CommandType.PROMPT, CommandType.APPROVE, CommandType.DENY)
        ):
            self._question_future.set_result(text.strip())
            return
        if cmd.type is CommandType.STOP:
            await self._cmd_stop(cmd.arg)
        elif cmd.type is CommandType.APPROVE:
            await self._cmd_permission_reply(True)
        elif cmd.type is CommandType.DENY:
            await self._cmd_permission_reply(False)
        elif cmd.type is CommandType.VERBOSE:
            await self._cmd_set_verbose(cmd.arg)
        elif cmd.type is CommandType.DEBUG:
            await self._cmd_set_dry_run(cmd.arg)
        elif cmd.type is CommandType.LIST_PROJECTS:
            await self._cmd_list_projects(cmd.arg)
        elif cmd.type is CommandType.SWITCH_PROJECT:
            await self._cmd_switch_project(cmd.arg)
        elif cmd.type is CommandType.STATUS:
            await self._cmd_status()
        elif cmd.type is CommandType.PWD:
            await self._cmd_pwd()
        elif cmd.type is CommandType.CLEAR:
            await self._cmd_clear()
        elif cmd.type is CommandType.QUEUE:
            await self._cmd_queue(cmd.arg)
        elif cmd.type is CommandType.UPDATE:
            await self._cmd_update()
        elif cmd.type is CommandType.HELP:
            await self._cmd_help(cmd.arg)
        elif cmd.type is CommandType.SESSION:
            await self._cmd_session()
        elif cmd.type is CommandType.RESUME:
            await self._cmd_resume(cmd.arg)
        elif cmd.type is CommandType.MODEL:
            await self._cmd_model(cmd.arg)
        elif cmd.type is CommandType.MODE:
            await self._cmd_mode(cmd.arg)
        elif cmd.type is CommandType.UNKNOWN:
            await self._cmd_unknown(cmd.arg or "")
        else:  # PROMPT
            await self._cmd_prompt(cmd.arg or "")

    async def handle_audio(self, recognition: str | None, sender_id: str) -> None:
        """Submit a voice message's transcription as a plain Claude turn.

        DingTalk transcribes voice messages itself (`recognition`); we never
        download the audio. The text is echoed back so the user can catch a
        misheard transcription, then run as an ordinary prompt — voice cannot
        reliably produce slash commands, so it skips command parsing.
        """
        if not self.is_authorized(sender_id):
            logger.warning("Ignoring message from unauthorized sender %s", sender_id)
            return
        text = (recognition or "").strip()
        if not text:
            await self._send(
                "🎤 Couldn't transcribe that voice message.\n"
                "Please resend it, or type your message instead."
            )
            return
        await self._send(f"🎤 Heard: {text}")
        await self._cmd_prompt(text)

    async def handle_image(self, prompt: str, sender_id: str) -> None:
        """Submit an image message — already rendered to a prompt — as a turn.

        The chat handler downloads the images and assembles `prompt` with local
        file paths Claude reads with the `Read` tool. It runs as an ordinary
        prompt: no slash-command parsing, same auth and queueing as a text one.
        """
        if not self.is_authorized(sender_id):
            logger.warning("Ignoring message from unauthorized sender %s", sender_id)
            return
        await self._cmd_prompt(prompt)

    async def _abort_running_task(self) -> bool:
        """Stop the in-flight turn — including a lingering background drain.

        interrupt() alone never ends the drain phase (it just reads the
        stream); the task must be cancelled too. _turn_cancelled then silences
        any event the task emits while it unwinds.
        """
        if self._task is None or self._task.done():
            return False
        # Flip the gate before the interrupt await: that await yields, and an
        # in-flight geo slow-notice timer could fire in the gap — it checks this
        # flag so it stays silent. It also silences any event the task emits
        # while it unwinds.
        self._turn_cancelled = True
        await self._runner.interrupt()
        self._cancel_pending_notice()
        self._task.cancel()
        return True

    async def _cmd_stop(self, arg: str | None = None) -> None:
        stop_all = arg is not None and arg.strip().lower() == "all"
        # Clear the queue BEFORE aborting: the cancelled turn's finally runs
        # _drain_queue, which would otherwise pop the next prompt and start it
        # — exactly the auto-advance `/stop all` is meant to prevent.
        dropped = 0
        if stop_all:
            dropped = len(self._queue)
            self._queue.clear()
        if self._permission_future is not None and not self._permission_future.done():
            self._permission_future.set_result(False)
            await self._send("🚫 Denied the pending operation.")
        if self._question_future is not None and not self._question_future.done():
            self._question_future.set_result(None)
        self._release_restart_confirm()
        task = self._task
        if task is not None and not task.done():
            # Without this line a /stop'd turn looks identical to a hang in
            # the log — the in-flight tool/assistant sequence just stops mid-
            # stream and no `result` ever lands. Mark the boundary explicitly.
            logger.info("turn interrupted reason=user_stop")
        if await self._abort_running_task():
            # Wait for the turn to fully unwind (disconnect, drain teardown)
            # before confirming — unless a queued prompt took over, which
            # already announced itself.
            if task is not None:  # pragma: no branch
                # False arm unreachable today: _abort_running_task returns
                # True only when self._task was set, and `task` was captured
                # from self._task without an intervening await. Kept as a
                # safety net for future reorderings that could insert an
                # await between the capture and abort.
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if self._task is None:
                if stop_all:
                    queue_note = (
                        f"\nDropped {dropped} queued prompt(s)." if dropped else ""
                    )
                    await self._send(
                        "✅ Task stopped and queue cleared."
                        f"{queue_note}\n"
                        "The session is kept — send a new prompt anytime."
                    )
                else:
                    msg = (
                        "✅ Task stopped.\n"
                        "The session is kept — send a new prompt anytime."
                    )
                    # The "go on" hint only makes sense once a turn has actually
                    # begun (i.e. the `▶️ Task started` banner went out). For a
                    # /stop landing in the geo phase, nothing was started so
                    # there's no "left off" to continue from.
                    if self._turn_announced:
                        msg += '\nOr say "go on" to continue where it left off.'
                    await self._send(msg)
        elif stop_all and dropped:
            await self._send(f"🧹 Cleared {dropped} queued prompt(s).")
        else:
            await self._send("ℹ️ No task is running.")

    def _release_restart_confirm(self) -> None:
        """Resolve a pending /update restart confirmation as 'skip'.

        /stop, /clear, and shutdown all mean the user abandoned the update;
        resolve the future as skip so it isn't left dangling (and can't later
        be mistaken for restart approval by _cmd_permission_reply)."""
        if self._restart_confirm is not None and not self._restart_confirm.done():
            self._restart_confirm.set_result(False)

    async def _cmd_permission_reply(self, approved: bool) -> None:
        # A pending /update restart confirmation takes priority. It exists only
        # while _updating is set, during which no turn can start (see
        # _cmd_prompt), so no tool-permission reply is ever in flight here — an
        # ok/no unambiguously answers the restart prompt.
        if self._restart_confirm is not None and not self._restart_confirm.done():
            self._restart_confirm.set_result(approved)
            return
        if self._permission_future is not None and not self._permission_future.done():
            self._permission_future.set_result(approved)
        else:
            await self._send("ℹ️ No pending operation to confirm.")

    async def _toggle(
        self,
        arg: str | None,
        attr: str,
        on_msg: str,
        off_msg: str,
        usage: str,
        label: str,
    ) -> None:
        val = (arg or "").lower()
        if val == "on":
            setattr(self, attr, True)
            await self._send(on_msg)
        elif val == "off":
            setattr(self, attr, False)
            await self._send(off_msg)
        else:
            state = "on" if getattr(self, attr) else "off"
            await self._send(
                f"{label} is {state}. \nUsage: {usage}"
            )

    async def _cmd_set_verbose(self, arg: str | None) -> None:
        await self._toggle(
            arg,
            "_verbose",
            on_msg="✅ Verbose mode on — showing tool calls and progress.",
            off_msg="✅ Verbose mode off — replies only, tool calls hidden.",
            usage="/verbose on|off",
            label="Verbose mode",
        )

    async def _cmd_set_dry_run(self, arg: str | None) -> None:
        await self._toggle(
            arg,
            "_dry_run",
            on_msg="🐛 Debug mode on — skipping Claude, echoing messages only.",
            off_msg="✅ Debug mode off.",
            usage="/debug on|off",
            label="Debug mode",
        )

    async def _cmd_list_projects(self, arg: str | None = None) -> None:
        verb = (arg or "").strip().lower()
        if verb == "reload":
            await self._reload_projects()
            return
        if verb:
            await self._send_markdown("ℹ️ Usage:\n" + HELP["ls"].detail)
            return
        lines = ["📂 **Projects**", ""]
        for name in self._registry.names():
            project = self._registry.get(name)
            mark = " *(current)*" if name == self._current_project.name else ""
            lines.append(
                f"- **{md_escape(name)}**{mark}<br>"
                f"{md_escape(display_path(project.path))}"
            )
        await self._send_markdown("\n".join(lines))

    async def _reload_projects(self) -> None:
        """Re-read the projects from the config file (after a manual edit) and
        list them — no daemon restart needed.

        Only the projects section is reloaded; the live dingtalk/geo config and
        session state are untouched. If the active project's name vanished
        (removed or renamed), fall back to the first project via the normal
        /cd path, which resets that session and announces the switch."""
        try:
            projects = load_projects(self._config_path)
        except ConfigError as exc:
            await self._send(f"⚠️ Reload failed — keeping current projects.\n{exc}")
            return
        self._registry = ProjectRegistry(projects)
        replacement = self._registry.get(self._current_project.name)
        if replacement is None:
            await self._cmd_switch_project(self._registry.default().name)
        else:
            # Re-point at the fresh object so an edited path takes effect.
            self._current_project = replacement
        await self._cmd_list_projects()

    async def _cmd_pwd(self) -> None:
        project = self._current_project
        await self._send(
            f"📂 {md_escape(display_path(project.path))}"
        )

    async def _cmd_switch_project(self, name: str | None) -> None:
        if not name:
            await self._send(
                'ℹ️ Usage: `/cd <project>` \nSend `/ls` to list projects.'
            )
            return
        project = self._registry.get(name)
        if project is None:
            await self._send(
                f'⚠️ Project "{md_escape(name)}" not found.\n'
                'Send `/ls` to list projects.'
            )
            return
        if self._task is not None and not self._task.done():
            await self._send(
                '⚠️ A task is running. Send `/stop` first, then switch.'
            )
            return
        self._current_project = project
        self._runner.reset(project.path)
        self._resume_candidates = []
        await self._send(
            f'📂 Switched to "{md_escape(project.name)}" — session reset.'
        )

    async def _cmd_status(self) -> None:
        running = self._task is not None and not self._task.done()
        state = "running" if running else "idle"
        if self._queue:
            state += f" (queue: {len(self._queue)})"
        project = self._current_project
        lines = [
            "🎛️ **Status**",
            "",
            f"- **State:** {state}",
            f"- **Project:** {md_escape(project.name)}",
            f"- **Output:** {'verbose' if self._verbose else 'brief'}",
            f"- **Model:** {short_model_name(self._runner.model_override or self._runner.observed_model or 'SDK default')}",
        ]
        if self._dry_run:
            lines.append("- **Debug:** on")
        tokens = self._runner.session_tokens(project.path)
        session_cost = self._runner.session_cost(project.path)
        if tokens or session_cost:
            # Cost-first when the SDK fed us a billed amount — that's what the
            # user actually cares about; tokens demote to a parenthetical so
            # the volume context is still visible. Falls back to tokens-first
            # when cost is unavailable (older SDK or aborted turn).
            if session_cost > 0:
                lines.append(
                    f"- **Session cost:** ~{format_cost(session_cost)} "
                    f"({format_tokens(tokens)} tokens)"
                )
            else:
                lines.append(f"- **Session tokens:** {format_tokens(tokens)}")
            # Per-model sub-bullets are only useful when more than one model
            # ran (main + subagents); for a single-model session they would
            # just restate the parent line. Sorted by token count desc so the
            # dominant consumer leads.
            model_tokens = self._runner.session_model_tokens(project.path)
            if len(model_tokens) > 1:
                for model, count in sorted(
                    model_tokens.items(), key=lambda kv: kv[1], reverse=True
                ):
                    lines.append(
                        f"  - {short_model_name(model)}: {format_tokens(count)}"
                    )
        usage = self._runner.last_usage(project.path)
        if usage:
            # When model_usage is present it's the authoritative source; the
            # snake_case usage dict only covers the main agent. Sum across
            # models so the parent line stays consistent with Session tokens
            # and the per-model sub-bullets add up to it.
            model_usage = self._runner.last_model_usage(project.path)
            if model_usage:
                b = _cache_breakdown(aggregate_model_usage(model_usage))
            else:
                b = _cache_breakdown(usage)
            last_turn_cost = self._runner.last_turn_cost(project.path)
            cost_prefix = (
                f"~{format_cost(last_turn_cost)} · "
                if last_turn_cost is not None
                else ""
            )
            lines.append(
                f"- **Cache last turn:** {cost_prefix}"
                f"cached {b['read']} ({b['hit']}) · new {b['creation']}"
            )
            if model_usage and len(model_usage) > 1:
                for model, entry in sorted(
                    model_usage.items(),
                    key=lambda kv: kv[1].get("cacheReadInputTokens", 0)
                    + kv[1].get("cacheCreationInputTokens", 0),
                    reverse=True,
                ):
                    mb = _model_cache_breakdown(entry)
                    lines.append(
                        f"  - {short_model_name(model)}: "
                        f"cached {mb['read']} ({mb['hit']}) · new {mb['creation']}"
                    )
        await self._send_markdown("\n".join(lines))

    async def _cmd_clear(self) -> None:
        self._release_restart_confirm()
        await self._abort_running_task()
        self._queue.clear()
        self._runner.reset(self._current_project.path)
        await self._send(
            "🧹 Interrupted the current task and reset the session.\n"
            "💬 The next message starts a fresh conversation."
        )

    async def _cmd_queue(self, arg: str | None) -> None:
        # split() collapses any run of interior whitespace, so "rm   3" and
        # "rm  all" parse the same as their single-space forms.
        parts = (arg or "").split()
        if not parts:
            await self._send_markdown(self._render_queue())
            return
        verb = parts[0].lower()
        rest = [p.lower() for p in parts[1:]]
        if verb == "clear" or (verb == "rm" and rest == ["all"]):
            if not self._queue:
                await self._send("📭 Queue is already empty.")
                return
            dropped = len(self._queue)
            self._queue.clear()
            await self._send(f"🗑 Cleared {dropped} queued prompt(s).")
            return
        if verb == "rm" and len(rest) == 1 and rest[0].isdigit():
            n = int(rest[0])
            if n < 1 or n > len(self._queue):
                await self._send(
                    f"⚠️ No queued prompt #{n} — "
                    f"the queue has {len(self._queue)}."
                )
                return
            removed = self._queue.pop(n - 1)
            await self._send(f"🗑 Removed #{n} from queue: {_summary(removed)}")
            return
        await self._send_markdown("⚠️ Usage:\n" + HELP["queue"].detail)

    def _render_queue(self) -> str:
        if not self._queue:
            return "📭 Queue is empty."
        lines = [f"📋 **Queue** · {len(self._queue)} waiting", MD_SPACER]
        for i, prompt in enumerate(self._queue, start=1):
            lines.append(f"{i}. {md_escape(_summary(prompt))}")
        lines.append(MD_SPACER)
        lines.append("💬 `/queue rm N` to drop one · `/queue clear` to empty")
        return "\n".join(lines)

    async def _cmd_update(self) -> None:
        """Update the daemon program itself: pull, re-setup/config on change,
        then confirm before restarting.

        Operates on the daemon's own repo (see self_update), unrelated to the
        user's projects. Refuses while a turn runs — like /cd and /resume —
        because the restart confirmation reuses the ok/no reply path. The
        _updating guard (set synchronously, no await before it) stops a second
        /update from racing the first into a concurrent pull/make.
        """
        if self._task is not None and not self._task.done():
            await self._send(
                "⚠️ A task is running. Send `/stop` first, then `/update`."
            )
            return
        if self._updating:
            await self._send("⚠️ An update is already in progress.")
            return
        self._updating = True
        try:
            await self._do_update()
        finally:
            self._updating = False

    async def _do_update(self) -> None:
        await self._send("🔄 Checking for updates…")
        try:
            status = await self_update.fetch_and_compare()
        except self_update.SelfUpdateError as exc:
            await self._send(f"⚠️ Update check failed\n{exc}")
            return
        if not status.behind:
            await self._send("✅ Already up to date.")
            return

        try:
            before = self_update.snapshot()
        except self_update.SelfUpdateError as exc:
            await self._send(f"⚠️ Update aborted\n{exc}")
            return
        subjects = "\n".join(status.subjects)
        await self._send(f"⬇️ Pulling {status.behind} commit(s):\n{subjects}")
        try:
            await self_update.pull()
        except self_update.SelfUpdateError as exc:
            await self._send(f"⚠️ git pull failed\n{exc}")
            return
        try:
            after = self_update.snapshot()
        except self_update.SelfUpdateError as exc:
            await self._send(
                f"⚠️ Pulled, but checking deps/config failed\n{exc}\n"
                "Run make daemon-restart when ready."
            )
            return

        if before.pyproject != after.pyproject:
            await self._send("📦 Dependencies changed — running make setup…")
            try:
                await self_update.run_make("setup")
            except self_update.SelfUpdateError as exc:
                await self._send(f"⚠️ make setup failed\n{exc}")
                return
            await self._send("✅ Dependencies installed.")
        if before.config_template != after.config_template:
            try:
                out = await self_update.run_make("config")
            except self_update.SelfUpdateError as exc:
                await self._send(f"⚠️ make config failed\n{exc}")
                return
            await self._send(f"⚙️ Config template changed:\n{out.strip()}")

        # Code changed, so a restart is needed regardless of deps/config —
        # confirm before the disruptive kickstart.
        loop = asyncio.get_running_loop()
        self._restart_confirm = loop.create_future()
        await self._send("✅ Update pulled. Reply `ok` to restart now, `no` to skip.")
        try:
            ok = await asyncio.wait_for(
                self._restart_confirm,
                timeout=self._config.permission_ask_timeout,
            )
        except asyncio.TimeoutError:
            await self._send(
                "⏱ Restart not confirmed — run make daemon-restart when ready."
            )
            return
        finally:
            self._restart_confirm = None
        if not ok:
            await self._send(
                "📌 Restart skipped — the new code loads on the next restart."
            )
            return
        await self._send("♻️ Restarting now — back in a few seconds.")
        self_update.trigger_restart_detached()

    async def _cmd_help(self, arg: str | None = None) -> None:
        name = (arg or "").strip().lstrip("/").lower()
        if name:
            entry = HELP.get(name)
            if entry is None:
                await self._send(
                    f"⚠️ Unknown command `{name}`. Send `/help` for the list."
                )
                return
            # Detail is the full explanation; the one-line brief is only the
            # fallback for commands too simple to warrant a detail block.
            content = entry.detail or f"{entry.brief}."
            await self._send_markdown(f"🛠 `{entry.syntax}`\n\n{content.strip()}")
            return
        lines = ["🛠 **Commands**", ""]
        for group in HELP_GROUPS:
            members = [e for e in HELP.values() if e.group == group]
            if not members:  # pragma: no cover - guards a future empty group
                continue
            lines.append(MD_SPACER)
            lines.append(f"**{group}**\n")
            for entry in members:
                lines.append(f"- `{entry.syntax}` — {entry.brief}")
        lines.append(MD_SPACER)
        lines.append("💬 `/help <command>` for one command's full usage")
        await self._send_markdown("\n".join(lines))

    async def _cmd_session(self) -> None:
        project = self._current_project
        session_id = self._runner.current_session(project.path)
        if not session_id:
            await self._send(
                "🧵 No session yet for this project.\n"
                "Send a message to start one."
            )
            return
        transcript = session_transcript_path(project.path, session_id)
        lines = [f"🧵 **Current session** · {md_escape(project.name)}"]
        lines.append(f"{MD_SPACER}")
        lines.append(f"**Session ID:**")
        lines.append(f"```\n{session_id}\n```")
        lines.append(f"{MD_SPACER}")
        lines.append(f"**Transcript:**")
        lines.append(f"```\n{display_path(transcript)}\n```")
        await self._send_markdown("\n".join(lines))

    async def _cmd_resume(self, arg: str | None) -> None:
        if self._task is not None and not self._task.done():
            await self._send('⚠️ A task is running. Send `/stop` first, then resume.')
            return
        project = self._current_project
        if not arg:
            infos = await list_recent_sessions(project.path, _RESUME_LIST_LIMIT)
            if not infos:
                await self._send("📋 No past sessions for this project.")
                return
            self._resume_candidates = [info.session_id for info in infos]
            current = self._runner.current_session(project.path)
            await self._send_markdown(format_session_list(infos, current))
            return
        session_id = await self._resolve_resume_arg(arg, project.path)
        if session_id is None:
            return
        self._runner.set_session(project.path, session_id)
        self._resume_candidates = []
        await self._send(
            f'🧵 Resumed session "{session_id[:8]}" · '
            f"{md_escape(project.name)}\n"
            "💬 The next message continues this conversation.\n"
            "⚠️ Close the TUI on your computer before driving from here."
        )

    async def _resolve_resume_arg(
        self, arg: str, project_path: str
    ) -> str | None:
        """Resolve a /resume argument to a session id, or None after sending
        an error to the phone."""
        if is_uuid(arg):
            info = await find_session(project_path, arg)
            if info is None:
                await self._send(
                    f'⚠️ Session "{arg[:8]}" not found in this project.'
                )
                return None
            return info.session_id
        if arg.isdigit():
            if not self._resume_candidates:
                await self._send('ℹ️ Send `/resume` first to see the list.')
                return None
            idx = int(arg)
            if idx < 1 or idx > len(self._resume_candidates):
                await self._send(
                    f"⚠️ Pick a number 1-{len(self._resume_candidates)},"
                    'or send `/resume` to refresh the list.'
                )
                return None
            return self._resume_candidates[idx - 1]
        await self._send_markdown("ℹ️ Usage:\n" + HELP["resume"].detail)
        return None

    def _format_model_list(self) -> str:
        override = self._runner.model_override
        known = set(MODEL_NAMES)
        lines = ["🤖 **Models**", ""]
        for name in MODEL_NAMES:
            mark = " *(current)*" if override == name else ""
            lines.append(f"- {name} {mark}")
        lines.append(MD_SPACER)
        # A list-external override (a full model id) gets its own line since
        # no list entry would be marked; otherwise show the observed default.
        # Model ids go in a code span — escaping would render as HTML entities.
        if override and override not in known:
            lines.append(f"Current: `{override}` (set via /model)")
        elif not override:
            observed = self._runner.observed_model
            if observed:
                lines.append(f"Current: `{observed}`")
            else:
                lines.append("Current: SDK default — send a prompt to detect it.")
        lines.append(MD_SPACER)
        lines.append('💬 `/model <name>` to switch\n')
        lines.append('🎏 Example: /model claude-opus-4-8[1m]')
        return "\n".join(lines)

    async def _cmd_model(self, arg: str | None) -> None:
        if not arg:
            await self._send_markdown(self._format_model_list())
            return
        self._runner.set_model(arg)
        await self._send(
            f'🤖 Model set to "{arg}" — takes effect next turn.'
        )

    async def _cmd_mode(self, arg: str | None) -> None:
        valid = ("acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan")
        if not arg:
            current = self._runner.permission_mode
            lines = ["🛡 **Permission modes**"]
            for name in valid:
                mark = " (current)" if name == current else ""
                lines.append(f"- `{name}`{mark}")
            lines.append("- `reset` — fall back to TUI's settings")
            lines.append(MD_SPACER)
            lines.append('💬 `/mode <name>` to switch')
            await self._send_markdown("\n".join(lines))
            return
        if arg.lower() == "reset":
            self._runner.set_permission_mode(None)
            await self._send(
                "🛡 Mode override cleared — next turn defaults to your TUI's settings."
            )
            return
        if arg not in valid:
            await self._send(
                f"⚠️ Unknown mode `{arg}`. Pick one of: "
                f"{', '.join(valid)}, or `reset`."
            )
            return
        self._runner.set_permission_mode(arg)
        await self._send(f'🛡 Mode set to "{arg}" — takes effect next turn.')

    async def _cmd_unknown(self, text: str) -> None:
        await self._send(
            f'❓ Unknown command: `{md_escape(text)}`\n'
            'Send `/help` for the command list.'
        )

    async def _cmd_prompt(self, prompt: str) -> None:
        if not prompt:
            return
        # No new turn may start mid-update: the restart that ends /update would
        # wipe the session anyway, and a running turn would let its ok/no be
        # mistaken for the restart confirmation. Refuse rather than queue.
        if self._updating:
            await self._send(
                "⚙️ Updating the daemon — please resend your message once it finishes."
            )
            return
        if self._task is not None and not self._task.done():
            # If the runner is just waiting for a missing background-agent
            # notification, the main turn is already done — a fresh prompt is
            # implicit permission to abandon that wait rather than sit behind
            # it for up to _STUCK_TIMEOUT seconds.
            if self._runner.is_draining:
                self._runner.cancel_drain()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
                # _run's finally may have popped a queued prompt and kicked
                # off another turn; fall back to normal queueing in that case
                # to preserve order.
                if self._task is not None and not self._task.done():
                    self._queue.append(prompt)
                    await self._send(
                        f"⏳ Task running — queued (#{len(self._queue)})."
                    )
                    return
            else:
                self._queue.append(prompt)
                await self._send(f"⏳ Task running — queued (#{len(self._queue)}).")
                return
        self._task = asyncio.create_task(self._run(prompt))

    async def _run(self, prompt: str) -> None:
        project = self._current_project
        # Each turn runs in its own asyncio task that forks the parent
        # context — wipe inherited stale labels before doing anything that
        # would render with them.
        log_context.clear()
        turn_num = self._runner.next_turn(project.path)
        # Banner the turn BEFORE stamping session/turn into log_context so the
        # formatter renders this line with no `session=… turn=…` prefix — the
        # turn number is already in the message text, and the session id isn't
        # known until the SDK init lands a moment later. Prompt preview goes
        # last so a long prompt doesn't push project/dry_run off the right
        # edge. _prompt_log_summary cuts at the nearest sentence boundary
        # between SOFT (80) and HARD (300), so a one-and-a-half-line prompt
        # isn't chopped mid-word — and short opening sentences get a second
        # sentence too instead of bare 20-char snippets.
        logger.info(
            'Running turn %d: project=%s dry_run=%s prompt="%s"',
            turn_num, project.name, self._dry_run, _prompt_log_summary(prompt),
        )
        log_context.set_turn(turn_num)
        log_context.set_session(self._runner.current_session(project.path))
        # cwd lets display.collapse_inline_paths shorten tool input paths
        # (`/long/proj/src/x` → `src/x`); paths outside the project but
        # inside $HOME render as `~/…`.
        log_context.set_cwd(project.path)
        # Outer try/finally so a /stop landing in the geo phase (or anywhere
        # before run_turn) still drains the queue — without this, self._task
        # stays set after cancel, the queued prompt doesn't auto-advance, and
        # _cmd_stop's `self._task is None` check never fires so the phone gets
        # zero feedback for the /stop.
        self._turn_announced = False
        # Reset here (not after the banner) so the geo-phase slow notice can read
        # this turn's own cancel state — a /stop landing in the geo await flips it
        # and the notice must honor that.
        self._turn_cancelled = False
        try:
            geo_note = ""
            if self._geo_check is not None:
                delay = _geo_slow_notice_delay(self._config.geo.timeout_seconds)
                slow_notice = (
                    asyncio.create_task(self._geo_slow_notice(delay))
                    if delay is not None
                    else None
                )
                try:
                    check = await self._geo_check()
                finally:
                    if slow_notice is not None:
                        slow_notice.cancel()
                if not check.ok:
                    await self._send(
                        f"{check.detail}\n⚠️ Turn skipped — fix the network and resend."
                    )
                    return
                geo_note = f"\n\n{check.detail}"
            if self._dry_run:
                await self._send(
                    f"🐛 Debug mode · {project.name}\n"
                    f"Echo: {_summary(prompt)}{geo_note}"
                )
                return
            await self._send(
                f"▶️ Task started · {project.name}\n{_summary(prompt)}{geo_note}"
            )
            self._turn_announced = True
            self._turn_sent_text = False
            self._last_todo_render = None
            self._pending_tasks = set()
            self._acknowledged_tasks = set()
            self._cancel_pending_notice()
            try:
                await self._runner.run_turn(project.path, prompt, self._emit)
            except Exception as exc:  # noqa: BLE001 - surface any failure to phone
                logger.exception("Task failed")
                await self._send(f"⚠️ Task failed\n{exc}")
        finally:
            await self._drain_queue()

    async def _drain_queue(self) -> None:
        if self._queue:
            nxt = self._queue.pop(0)
            self._task = asyncio.create_task(self._run(nxt))
        else:
            self._task = None

    async def _emit(self, event) -> None:
        # A stopped/cleared turn keeps unwinding (disconnect, drain teardown);
        # drop anything it still emits so stale messages never reach the phone.
        if self._turn_cancelled:
            return
        if isinstance(event, TextEvent):
            # Brief mode drops pure forward-looking narration ("Now the X.",
            # "Let me read Y.") — the bulk of per-turn message volume — while
            # keeping any block that reports a finding or result. Verbose mode
            # shows everything; the ResultEvent fallback still surfaces the
            # SDK's final `result` if a whole turn filtered out.
            if _is_progress_filler(event.text) and not self._verbose:
                return
            self._turn_sent_text = True
            await self._send_markdown(event.text)
        elif isinstance(event, ToolEvent):
            if self._verbose:
                suffix = f" · {event.summary}" if event.summary else ""
                await self._send(f"🔧 {event.name}{suffix}")
        elif isinstance(event, ResultEvent):
            # The reply already went out block by block. Fall back to the
            # ResultMessage text only when the turn produced no text at all.
            if not self._turn_sent_text:
                await self._send_markdown(
                    event.text.strip() or "(empty)"
                )
            self._arm_pending_notice()
        elif isinstance(event, TodoEvent):
            # The checklist is the headline progress signal — pushed even in
            # brief mode, deduped so repeated identical snapshots stay quiet.
            rendered = render_todos(event.items)
            if rendered != self._last_todo_render:
                self._last_todo_render = rendered
                await self._send_markdown(rendered)
        elif isinstance(event, TaskEvent):
            await self._emit_task(event)

    async def _emit_task(self, event: TaskEvent) -> None:
        # A started/notification pair within the turn cancels out; an id left
        # in _pending_tasks at turn end means a detached agent outlived it.
        if event.phase == "started":
            self._pending_tasks.add(event.task_id)
            await self._send(f"🚀 Subagent started · {event.description}")
        elif event.phase == "acknowledged":
            # SDK marked the task terminal — bookkeeping only, no phone
            # message (no duration/token data here to be worth surfacing).
            # Re-arm the notice so any pending timer reflects the smaller
            # "truly pending" set.
            self._acknowledged_tasks.add(event.task_id)
            self._arm_pending_notice()
        elif event.phase == "notification":
            self._pending_tasks.discard(event.task_id)
            self._arm_pending_notice()
            # SDK summary for completed agents already reads like
            # `Agent "X" completed`; for failed/stopped it likewise names the
            # task — using only summary avoids the description double-print.
            body = event.summary or event.description or "Subagent"
            usage = _fmt_usage(event.duration_ms, event.total_tokens)
            icon = {"completed": "✅", "failed": "❌", "stopped": "⏹"}.get(
                event.status, "ℹ️"
            )
            await self._send(f"{icon} {body}{usage}")
        elif event.phase == "timeout":
            self._pending_tasks.discard(event.task_id)
            self._arm_pending_notice()
            await self._send(
                "⏱ A background agent hasn't reported back — "
                "send a message to have Claude check on it."
            )

    async def _geo_slow_notice(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        # A /stop that landed during the geo await flips _turn_cancelled (before
        # it interrupts), so don't push a "still checking" reassurance moments
        # before the "Task stopped" ack.
        if self._turn_cancelled:
            return
        await self._send(
            "⏳ Checking geo location — this is taking a moment, please wait."
        )

    def _cancel_pending_notice(self) -> None:
        if self._pending_notice_task is not None:
            self._pending_notice_task.cancel()
            self._pending_notice_task = None

    def _arm_pending_notice(self) -> None:
        """(Re)start the delayed-notice timer based on current pending state.

        Only counts subagents the SDK hasn't acknowledged yet — an
        acknowledged task is known-done at the SDK level, so warning about it
        would just confuse the user. Always cancels the previous timer first;
        if no un-acknowledged pending remain we leave it cancelled. Called
        from ResultEvent, notification, acknowledged and timeout paths, so
        the timer always reflects the latest "truly running" set without
        needing per-call branching at the caller.
        """
        self._cancel_pending_notice()
        if not (self._pending_tasks - self._acknowledged_tasks):
            return
        self._pending_notice_task = asyncio.create_task(
            self._delayed_pending_notice()
        )

    async def _delayed_pending_notice(self) -> None:
        try:
            await asyncio.sleep(_PENDING_NOTICE_DELAY)
        except asyncio.CancelledError:
            return
        # Recheck after sleep: a notification or acknowledgement that arrived
        # right as the timer was firing may have shrunk the un-acknowledged
        # set before cancel() could land at an await point. Belt to the
        # cancel suspenders.
        truly_pending = self._pending_tasks - self._acknowledged_tasks
        if not truly_pending or self._turn_cancelled:
            return
        n = len(truly_pending)
        plural = "s" if n > 1 else ""
        await self._send(
            f"⏳ {n} background agent{plural} still running — "
            "I'll push the result here when it finishes."
        )

    async def request_permission(
        self, tool_name: str, tool_input: dict, project_path: str | None = None
    ) -> bool:
        summary = tool_summary(tool_name, tool_input)
        input_preview = _summary(summary) if summary else ""
        # Escalations are serialized: parallel tool calls each get their own
        # prompt-and-wait rather than clobbering a shared pending future.
        async with self._permission_lock:
            # Wrap the summary in a fenced code block so a heredoc body with
            # a `# …` Python/shell comment line doesn't get rendered as an H1.
            body = f"{tool_name}:\n```\n{summary}\n```" if summary else tool_name
            loop = asyncio.get_running_loop()
            self._permission_future = loop.create_future()
            chip = f"{tool_name}#{secrets.token_hex(4)}"
            logger.info(
                'permission escalate tool=%s input="%s" → phone',
                chip, input_preview,
            )
            await self._send_markdown(
                f'### 🔐 Permission needed\n{MD_SPACER}\n{body}\n{MD_SPACER}\n\nReply `👌(ok)` to allow, `❌(no)` to deny.'
            )
            start = loop.time()
            try:
                allowed = await asyncio.wait_for(
                    self._permission_future,
                    timeout=self._config.permission_ask_timeout,
                )
                logger.info(
                    "permission reply tool=%s waited=%.1fs result=%s",
                    chip,
                    loop.time() - start,
                    "allowed" if allowed else "denied",
                )
                return allowed
            except asyncio.TimeoutError:
                logger.info(
                    "permission timeout tool=%s waited=%.1fs result=denied",
                    chip, loop.time() - start,
                )
                await self._send("⏱ Permission request timed out — denied.")
                return False
            finally:
                self._permission_future = None

    async def answer_question(self, tool_input: dict, project_path: str) -> str:
        """Push an AskUserQuestion to the phone and return the user's answer.

        The returned string is delivered to Claude as the tool result via
        PermissionResultDeny(message=...), so it must read as a self-contained
        instruction.
        """
        questions = tool_input.get("questions") or []
        if not questions:
            logger.info("ask_user_question empty questions list — skipping")
            return "No question was provided; continue without an answer."
        loop = asyncio.get_running_loop()
        start = loop.time()
        logger.info(
            "ask_user_question count=%d first=%r",
            len(questions),
            _summary(question_preview(questions[0])),
        )
        async with self._permission_lock:
            answers: list[str] = []
            for idx, question in enumerate(questions):
                options = question.get("options") or []
                label = question_label(question) or "Question"
                reply = await self._ask_one(question, idx, len(questions), options)
                if reply is None:
                    logger.info(
                        "ask_user_question cancelled idx=%d/%d waited=%.1fs",
                        idx + 1, len(questions), loop.time() - start,
                    )
                    return (
                        "The user did not answer your AskUserQuestion via "
                        "DingTalk (cancelled or timed out). Do not retry; "
                        "proceed with a reasonable default."
                    )
                answers.append(f"- {label}: {reply}")
            logger.info(
                "ask_user_question answered count=%d waited=%.1fs",
                len(questions), loop.time() - start,
            )
            return (
                "The user answered your AskUserQuestion via DingTalk:\n"
                + "\n".join(answers)
                + "\nContinue based on these answers."
            )

    async def _ask_one(
        self, question: dict, idx: int, total: int, options: list[dict]
    ) -> str | None:
        """Send one question, wait for a reply, return the answer or None."""
        while True:
            await self._send(format_question(question, idx, total))
            loop = asyncio.get_running_loop()
            self._question_future = loop.create_future()
            try:
                reply = await asyncio.wait_for(
                    self._question_future,
                    timeout=self._config.permission_ask_timeout,
                )
            except asyncio.TimeoutError:
                await self._send("⏱ Question timed out — no answer recorded.")
                return None
            finally:
                self._question_future = None
            if reply is None:  # cancelled by /stop
                return None
            answer, valid = parse_answer(reply, options)
            if valid:
                return answer
            await self._send(
                f"That number is out of range — pick 1-{len(options)}, "
                "or type your own answer."
            )
