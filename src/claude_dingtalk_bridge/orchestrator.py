from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Awaitable, Callable

from claude_dingtalk_bridge.claude_runner import (
    MODEL_CHOICES,
    ResultEvent,
    TaskEvent,
    TextEvent,
    TodoEvent,
    ToolEvent,
    _cache_breakdown,
    tool_summary,
)
from claude_dingtalk_bridge.commands import CommandType, parse_command
from claude_dingtalk_bridge.config import Config
from claude_dingtalk_bridge.geo import GeoCheck
from claude_dingtalk_bridge import log_context
from claude_dingtalk_bridge.permissions import Decision, PermissionPolicy
from claude_dingtalk_bridge.projects import ProjectRegistry
from claude_dingtalk_bridge.questions import format_question, parse_answer
from claude_dingtalk_bridge.display import (
    display_path,
    format_tokens,
    md_escape,
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


def _looks_like_narration(text: str) -> bool:
    """A text block ending in colon is almost always pre-tool intent narration.

    Empirically every "now I'll do X" line the assistant emits between tool
    calls ends in `:` (English) or `：` (Chinese), while content/reply text
    ends in a sentence terminator. This is a deliberate heuristic, not a hard
    guarantee — verbose mode bypasses it and ResultMessage acts as a safety
    net when an entire turn was filtered out.

    Trailing markdown emphasis (``**bold:**``, ``_italic:_``) is stripped
    before the check — otherwise a bolded narration line ends in ``**`` and
    slips past, which historically reached the phone.
    """
    return text.rstrip().rstrip("*_").endswith((":", "："))


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
        policy: PermissionPolicy,
        runner,
        send: Send,
        send_markdown: Send,
        geo_check: Callable[[], Awaitable[GeoCheck]] | None = None,
    ):
        self._config = config
        self._registry = registry
        self._policy = policy
        self._runner = runner
        self._send = send
        # Command replies and Claude-authored text are rendered as markdown.
        # Runtime task/permission messages keep send — their line-by-line
        # layout would collapse under DingTalk's markdown newline folding.
        self._send_markdown = send_markdown
        self._verbose = False
        self._current_project = registry.default()
        self._task: asyncio.Task | None = None
        self._queue: list[str] = []
        self._permission_future: asyncio.Future[bool] | None = None
        self._permission_lock = asyncio.Lock()
        self._question_future: asyncio.Future[str | None] | None = None
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
        self._last_todo_render: str | None = None
        self._pending_tasks: set[str] = set()
        self._acknowledged_tasks: set[str] = set()
        self._pending_notice_task: asyncio.Task | None = None

    def is_authorized(self, sender_id: str) -> bool:
        """True only for the single configured authorized DingTalk user."""
        return sender_id == self._config.authorized_user_id

    async def notify(self, message: str) -> None:
        """Push an out-of-band markdown notice to the phone.

        For daemon-level paths (image download failures, shutdown notices)
        that need to reach the user without going through a Claude turn.
        """
        await self._send_markdown(message)

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
            await self._cmd_stop()
        elif cmd.type is CommandType.APPROVE:
            await self._cmd_permission_reply(True)
        elif cmd.type is CommandType.DENY:
            await self._cmd_permission_reply(False)
        elif cmd.type is CommandType.VERBOSE:
            await self._cmd_set_verbose(cmd.arg)
        elif cmd.type is CommandType.DEBUG:
            await self._cmd_set_dry_run(cmd.arg)
        elif cmd.type is CommandType.LIST_PROJECTS:
            await self._cmd_list_projects()
        elif cmd.type is CommandType.SWITCH_PROJECT:
            await self._cmd_switch_project(cmd.arg)
        elif cmd.type is CommandType.STATUS:
            await self._cmd_status()
        elif cmd.type is CommandType.PWD:
            await self._cmd_pwd()
        elif cmd.type is CommandType.CLEAR:
            await self._cmd_clear()
        elif cmd.type is CommandType.HELP:
            await self._cmd_help()
        elif cmd.type is CommandType.SESSION:
            await self._cmd_session()
        elif cmd.type is CommandType.RESUME:
            await self._cmd_resume(cmd.arg)
        elif cmd.type is CommandType.MODEL:
            await self._cmd_model(cmd.arg)
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
            await self._send_markdown(
                "🎤 Couldn't transcribe that voice message.  \n"
                "Please resend it, or type your message instead."
            )
            return
        await self._send_markdown(f"🎤 Heard: {md_escape(text)}")
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
        await self._runner.interrupt()
        self._turn_cancelled = True
        self._cancel_pending_notice()
        self._task.cancel()
        return True

    async def _cmd_stop(self) -> None:
        if self._permission_future is not None and not self._permission_future.done():
            self._permission_future.set_result(False)
            await self._send_markdown("🚫 Denied the pending operation.")
        if self._question_future is not None and not self._question_future.done():
            self._question_future.set_result(None)
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
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if self._task is None:
                await self._send_markdown(
                    "✅ Task stopped.  \n"
                    "- The session is kept — send a new prompt anytime.\n"
                    "- Or say `go on` to continue where it left off."
                )
        else:
            await self._send_markdown("ℹ️ No task is running.")

    async def _cmd_permission_reply(self, approved: bool) -> None:
        if self._permission_future is not None and not self._permission_future.done():
            self._permission_future.set_result(approved)
        else:
            await self._send_markdown("ℹ️ No pending operation to confirm.")

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
            await self._send_markdown(on_msg)
        elif val == "off":
            setattr(self, attr, False)
            await self._send_markdown(off_msg)
        else:
            state = "on" if getattr(self, attr) else "off"
            await self._send_markdown(
                f"ℹ️ {label} is **{state}**.  \nUsage: `{usage}`"
            )

    async def _cmd_set_verbose(self, arg: str | None) -> None:
        await self._toggle(
            arg,
            "_verbose",
            on_msg="✅ **Verbose mode on** — showing tool calls and progress.",
            off_msg="✅ **Verbose mode off** — replies only, tool calls hidden.",
            usage="/verbose on|off",
            label="Verbose mode",
        )

    async def _cmd_set_dry_run(self, arg: str | None) -> None:
        await self._toggle(
            arg,
            "_dry_run",
            on_msg="🐛 **Debug mode on** — skipping Claude, echoing messages only.",
            off_msg="✅ **Debug mode off**.",
            usage="/debug on|off",
            label="Debug mode",
        )

    async def _cmd_list_projects(self) -> None:
        lines = ["📂 **Projects**", ""]
        for name in self._registry.names():
            project = self._registry.get(name)
            mark = " *(current)*" if name == self._current_project.name else ""
            lines.append(
                f"- **{md_escape(name)}**{mark} · "
                f"{md_escape(display_path(project.path))}"
            )
        await self._send_markdown("\n".join(lines))

    async def _cmd_pwd(self) -> None:
        project = self._current_project
        await self._send_markdown(
            f"📂 **{md_escape(project.name)}**  \n"
            f"{md_escape(display_path(project.path))}"
        )

    async def _cmd_switch_project(self, name: str | None) -> None:
        if not name:
            await self._send_markdown(
                "ℹ️ Usage: `/cd <project>`. Send `/ls` to list projects."
            )
            return
        project = self._registry.get(name)
        if project is None:
            await self._send_markdown(
                f'⚠️ Project "{md_escape(name)}" not found. '
                "Send `/ls` to list projects."
            )
            return
        if self._task is not None and not self._task.done():
            await self._send_markdown(
                "⚠️ A task is running. Send `/stop` first, then switch."
            )
            return
        self._current_project = project
        self._runner.reset(project.path)
        self._resume_candidates = []
        await self._send_markdown(
            f"📂 Switched to **{md_escape(project.name)}** — session reset."
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
            f"- **Model:** {self._runner.model_override or self._runner.observed_model or 'default'}",
        ]
        if self._dry_run:
            lines.append("- **Debug:** on")
        tokens = self._runner.session_tokens(project.path)
        lines.append(f"- **Session tokens:** {format_tokens(tokens)}")
        usage = self._runner.last_usage(project.path)
        if usage:
            b = _cache_breakdown(usage)
            lines.append(
                f"- **Cache last turn:** read {b['read']} (hit {b['hit']}) · creation {b['creation']}"
            )
        await self._send_markdown("\n".join(lines))

    async def _cmd_clear(self) -> None:
        await self._abort_running_task()
        self._queue.clear()
        self._runner.reset(self._current_project.path)
        await self._send_markdown(
            "🧹 Interrupted the current task and reset the session.  \n"
            "The next message starts a fresh conversation."
        )

    async def _cmd_help(self) -> None:
        await self._send_markdown(
            "🛠 **Commands**\n\n"
            "**Task**\n\n"
            "- `/stop` — interrupt the current task\n"
            "- `/clear` — interrupt & reset the session\n\n"
            "**Project**\n\n"
            "- `/pwd` — show the current project\n"
            "- `/ls` — list projects\n"
            "- `/cd <name>` — switch project\n\n"
            "**Session**\n\n"
            "- `/session` — show the current session id\n"
            "- `/resume` — list recent sessions\n"
            "- `/resume <n>` — switch to a listed session\n"
            "- `/compact` — compact the conversation history\n\n"
            "**Info**\n\n"
            "- `/help` — show this help\n"
            "- `/status` — show runtime status\n"
            "- `/context` — show context window usage\n"
            "- `/usage` — show usage and cost\n\n"
            "**Modes**\n\n"
            "- `/model` — list models\n"
            "- `/model <n | name>` — switch model\n"
            "- `/verbose on|off` — stream every step\n"
            "- `/debug on|off` — skip Claude, debug the daemon only\n\n"
            "Reply `approve` / `reject` to a permission prompt."
        )

    async def _cmd_session(self) -> None:
        project = self._current_project
        session_id = self._runner.current_session(project.path)
        if not session_id:
            await self._send_markdown(
                "🧵 **No session yet** for this project.  \n"
                "Send a message to start one."
            )
            return
        transcript = session_transcript_path(project.path, session_id)
        await self._send_markdown(
            f"🧵 **Current session** · {md_escape(project.name)}\n\n"
            f"**Session ID:**\n"
            f"```\n{session_id}\n```\n\n"
            f"**Transcript:**\n"
            f"```\n{display_path(transcript)}\n```"
        )

    async def _cmd_resume(self, arg: str | None) -> None:
        if self._task is not None and not self._task.done():
            await self._send_markdown(
                "⚠️ A task is running. Send `/stop` first, then resume."
            )
            return
        project = self._current_project
        if not arg:
            infos = await list_recent_sessions(project.path, _RESUME_LIST_LIMIT)
            if not infos:
                await self._send_markdown(
                    "📋 No past sessions for this project."
                )
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
        await self._send_markdown(
            f"🧵 Resumed session `{session_id[:8]}` · "
            f"{md_escape(project.name)}  \n"
            "The next message continues this conversation.  \n"
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
                await self._send_markdown(
                    f"⚠️ Session `{arg[:8]}` not found in this project."
                )
                return None
            return info.session_id
        if arg.isdigit():
            if not self._resume_candidates:
                await self._send_markdown(
                    "ℹ️ Send `/resume` first to see the list."
                )
                return None
            idx = int(arg)
            if idx < 1 or idx > len(self._resume_candidates):
                await self._send_markdown(
                    f"⚠️ Pick a number 1-{len(self._resume_candidates)}, "
                    "or send `/resume` to refresh the list."
                )
                return None
            return self._resume_candidates[idx - 1]
        await self._send_markdown(
            "ℹ️ Usage: `/resume`, `/resume <number>`, or `/resume <session-id>`."
        )
        return None

    def _format_model_list(self) -> str:
        override = self._runner.model_override
        known = {name for name, _ in MODEL_CHOICES}
        lines = ["🤖 **Models**", ""]
        for idx, (name, desc) in enumerate(MODEL_CHOICES, start=1):
            mark = " *(current)*" if override == name else ""
            lines.append(f"- **{idx}. {name}**{mark} — {desc}")
        lines.append("")
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
                lines.append(
                    "Current: SDK default — send a message to detect it."
                )
        lines.append("")
        lines.append("💬 `/model <n>` or `/model <name>` to switch")
        return "\n".join(lines)

    async def _cmd_model(self, arg: str | None) -> None:
        if not arg:
            await self._send_markdown(self._format_model_list())
            return
        if arg.isdigit():
            idx = int(arg)
            if idx < 1 or idx > len(MODEL_CHOICES):
                await self._send_markdown(
                    f"⚠️ Pick a number 1-{len(MODEL_CHOICES)}, "
                    "or send `/model` to see the list."
                )
                return
            name = MODEL_CHOICES[idx - 1][0]
        else:
            name = arg
        self._runner.set_model(name)
        await self._send_markdown(
            f"🤖 Model set to **{md_escape(name)}** — takes effect next turn."
        )

    async def _cmd_unknown(self, text: str) -> None:
        await self._send_markdown(
            f"❓ Unknown command: **{md_escape(text)}**  \n"
            "Send `/help` for the command list."
        )

    async def _cmd_prompt(self, prompt: str) -> None:
        if not prompt:
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
        geo_note = ""
        if self._geo_check is not None:
            check = await self._geo_check()
            if not check.ok:
                await self._send(
                    f"{check.detail}\n⚠️ Turn skipped — fix the network and resend."
                )
                await self._drain_queue()
                return
            geo_note = f"\n\n{check.detail}"
        if self._dry_run:
            await self._send(
                f"🐛 Debug mode · {project.name}\n"
                f"Echo: {_summary(prompt)}{geo_note}"
            )
            await self._drain_queue()
            return
        await self._send(
            f"▶️ Task started · {project.name}\n{_summary(prompt)}{geo_note}"
        )
        self._turn_cancelled = False
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
            # Heuristic: a text block ending in `:` / `：` is pre-tool narration
            # ("now I'll do X:"). Content text ends in a sentence terminator
            # (`.`, `。`, `!`, `?` …). Drop narration in non-verbose mode; the
            # fallback at ResultEvent still surfaces the SDK's final `result`
            # if the entire turn was narration-only.
            if _looks_like_narration(event.text) and not self._verbose:
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
                    event.text.strip() or "(no text output)"
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
        self, tool_name: str, tool_input: dict, project_path: str
    ) -> bool:
        decision = self._policy.evaluate(tool_name, tool_input, project_path)
        summary = tool_summary(tool_name, tool_input)
        input_preview = _summary(summary) if summary else ""
        if decision is Decision.ALLOW:
            # Auto-allows are the common case — log at DEBUG to keep INFO clean
            # while still allowing "why did this go through?" forensics.
            logger.debug(
                "permission auto-allow tool=%s input=%r", tool_name, input_preview
            )
            return True
        # Escalations are serialized: parallel tool calls each get their own
        # prompt-and-wait rather than clobbering a shared pending future.
        async with self._permission_lock:
            desc = f"{tool_name} · {summary}" if summary else tool_name
            loop = asyncio.get_running_loop()
            self._permission_future = loop.create_future()
            logger.info(
                'permission escalate tool=%s input="%s" → phone',
                tool_name, input_preview,
            )
            await self._send(
                f"🔐 Permission needed\n{desc}\nReply ok to allow, no to deny."
            )
            start = loop.time()
            try:
                allowed = await asyncio.wait_for(
                    self._permission_future,
                    timeout=self._config.permission_timeout_seconds,
                )
                logger.info(
                    "permission reply tool=%s waited=%.1fs result=%s",
                    tool_name,
                    loop.time() - start,
                    "allowed" if allowed else "denied",
                )
                return allowed
            except asyncio.TimeoutError:
                logger.warning(
                    "permission timeout tool=%s waited=%.1fs result=denied",
                    tool_name, loop.time() - start,
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
            _summary(questions[0].get("question") or questions[0].get("header") or ""),
        )
        async with self._permission_lock:
            answers: list[str] = []
            for idx, question in enumerate(questions):
                options = question.get("options") or []
                label = (
                    question.get("header")
                    or question.get("question")
                    or "Question"
                )
                reply = await self._ask_one(question, idx, len(questions), options)
                if reply is None:
                    logger.warning(
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
                    timeout=self._config.permission_timeout_seconds,
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
