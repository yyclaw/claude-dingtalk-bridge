import asyncio
import contextlib
from pathlib import Path

import pytest

import claude_dingtalk_bridge.orchestrator as orch_mod
from claude_dingtalk_bridge.claude_runner import ResultEvent, TextEvent, ToolEvent
from claude_dingtalk_bridge.config import Config, PermissionRules, Project
from claude_dingtalk_bridge.orchestrator import Orchestrator
from claude_dingtalk_bridge.permissions import PermissionPolicy
from claude_dingtalk_bridge.projects import ProjectRegistry

AUTHORIZED = "staff-1"


class FakeInfo:
    def __init__(
        self,
        session_id,
        summary="s",
        last_modified=0,
        git_branch=None,
        file_size=1234,
    ):
        self.session_id = session_id
        self.summary = summary
        self.last_modified = last_modified
        self.git_branch = git_branch
        self.file_size = file_size


def make_config() -> Config:
    return Config(
        dingtalk_client_id="k",
        dingtalk_client_secret="s",
        authorized_user_id=AUTHORIZED,
        projects=[
            Project(name="multica", path="/tmp/multica"),
            Project(name="docs", path="/tmp/docs"),
        ],
        permissions=PermissionRules(
            allowed_tools=["Read"], allowed_bash=["git status"],
            allow_edits_in_project=True,
        ),
        permission_timeout_seconds=600,
    )


class FakeRunner:
    """Stand-in for ClaudeRunner. run_turn replays scripted events."""

    def __init__(self):
        self.permission_handler = None
        self.question_handler = None
        self.turns: list[tuple[str, str]] = []
        self.interrupts = 0
        self.resets: list[str] = []
        self.script: list = []
        self.sessions: dict[str, str] = {}
        self.token_totals: dict[str, int] = {}
        self.usages: dict[str, dict] = {}
        self.model_token_totals: dict[str, dict[str, int]] = {}
        self.model_usages: dict[str, dict] = {}
        self.model_override = None
        self.observed_model = None
        self.is_draining = False
        self.drain_cancels = 0
        self.turn_counts: dict[str, int] = {}

    def cancel_drain(self) -> None:
        self.drain_cancels += 1
        self.is_draining = False

    def set_model(self, model):
        self.model_override = model

    def reset(self, project_path: str) -> None:
        self.resets.append(project_path)

    def set_session(self, project_path: str, session_id: str) -> None:
        self.sessions[project_path] = session_id

    def current_session(self, project_path: str) -> str | None:
        return self.sessions.get(project_path)

    def next_turn(self, project_path: str) -> int:
        nxt = self.turn_counts.get(project_path, 0) + 1
        self.turn_counts[project_path] = nxt
        return nxt

    def session_tokens(self, project_path: str) -> int:
        return self.token_totals.get(project_path, 0)

    def last_usage(self, project_path: str) -> dict | None:
        return self.usages.get(project_path)

    def session_model_tokens(self, project_path: str) -> dict[str, int]:
        return dict(self.model_token_totals.get(project_path, {}))

    def last_model_usage(self, project_path: str) -> dict | None:
        return self.model_usages.get(project_path)

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def run_turn(self, project_path, prompt, emit):
        self.turns.append((project_path, prompt))
        for event in self.script:
            await emit(event)
        # Every real turn ends with a ResultMessage; mirror that so the
        # orchestrator sends the turn's reply. Scripts may supply their own.
        if not any(isinstance(e, ResultEvent) for e in self.script):
            await emit(ResultEvent("", False))
        self.script = []


def build(runner=None, geo_check=None):
    config = make_config()
    runner = runner or FakeRunner()
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    orchestrator = Orchestrator(
        config=config,
        registry=ProjectRegistry(config.projects),
        policy=PermissionPolicy(config.permissions),
        runner=runner,
        send=send,
        send_markdown=send,
        geo_check=geo_check,
    )
    runner.permission_handler = orchestrator.request_permission
    runner.question_handler = orchestrator.answer_question
    return orchestrator, runner, sent


def build_channels(runner=None):
    """Like build() but with separate text and markdown send channels."""
    config = make_config()
    runner = runner or FakeRunner()
    text_sent: list[str] = []
    md_sent: list[str] = []

    async def send(text: str) -> None:
        text_sent.append(text)

    async def send_markdown(text: str) -> None:
        md_sent.append(text)

    orchestrator = Orchestrator(
        config=config,
        registry=ProjectRegistry(config.projects),
        policy=PermissionPolicy(config.permissions),
        runner=runner,
        send=send,
        send_markdown=send_markdown,
    )
    runner.permission_handler = orchestrator.request_permission
    runner.question_handler = orchestrator.answer_question
    return orchestrator, runner, text_sent, md_sent


async def _wait_idle(orchestrator):
    while orchestrator._task is not None and not orchestrator._task.done():
        await asyncio.sleep(0)
    if orchestrator._task is not None:
        await orchestrator._task


async def test_unauthorized_sender_ignored():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("run my tests", "intruder-99")
    assert sent == []
    assert runner.turns == []


async def test_audio_message_echoes_and_runs_turn():
    orchestrator, runner, sent = build()
    await orchestrator.handle_audio("fix the login bug", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert any("🎤 Heard:" in m and "fix the login bug" in m for m in sent)
    assert runner.turns == [("/tmp/multica", "fix the login bug")]


async def test_audio_empty_recognition_replies_hint_without_turn():
    orchestrator, runner, sent = build()
    await orchestrator.handle_audio("", AUTHORIZED)
    assert runner.turns == []
    assert any("transcribe" in m.lower() for m in sent)
    assert not any("🎤 Heard:" in m for m in sent)


async def test_audio_missing_recognition_replies_hint_without_turn():
    orchestrator, runner, sent = build()
    await orchestrator.handle_audio(None, AUTHORIZED)
    assert runner.turns == []
    assert any("transcribe" in m.lower() for m in sent)


async def test_audio_recognition_is_never_parsed_as_command():
    orchestrator, runner, sent = build()
    await orchestrator.handle_audio("/stop the server", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert runner.turns == [("/tmp/multica", "/stop the server")]


async def test_audio_unauthorized_sender_ignored():
    orchestrator, runner, sent = build()
    await orchestrator.handle_audio("run my tests", "intruder-99")
    assert sent == []
    assert runner.turns == []


async def test_image_message_runs_turn_with_verbatim_prompt():
    orchestrator, runner, sent = build()
    prompt = "I sent you an image: [image saved at /tmp/a.png]. Please take a look."
    await orchestrator.handle_image(prompt, AUTHORIZED)
    await _wait_idle(orchestrator)
    assert runner.turns == [("/tmp/multica", prompt)]


async def test_image_message_queued_while_task_running():
    orchestrator, runner, sent = build()
    orchestrator._task = asyncio.create_task(asyncio.sleep(100))
    await orchestrator.handle_image("look at [image saved at /tmp/a.png]", AUTHORIZED)
    assert orchestrator._queue == ["look at [image saved at /tmp/a.png]"]
    orchestrator._task.cancel()


async def test_image_message_unauthorized_sender_ignored():
    orchestrator, runner, sent = build()
    await orchestrator.handle_image("look at [image saved at /tmp/a.png]", "intruder-99")
    assert sent == []
    assert runner.turns == []


async def test_prompt_runs_a_turn():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("fix the login bug", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert runner.turns == [("/tmp/multica", "fix the login bug")]
    assert any("Task started" in m for m in sent)
    # The turn's reply is sent on its own — no "Done"-style header.
    assert any("(no text output)" in m for m in sent)
    assert not any("Done" in m for m in sent)


async def test_status_reports_idle():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/status", AUTHORIZED)
    assert any("idle" in m and "multica" in m for m in sent)


async def test_switch_to_unknown_project():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/cd nope", AUTHORIZED)
    assert any("not found" in m for m in sent)
    assert runner.resets == []


async def test_switch_project_resets_session():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/cd docs", AUTHORIZED)
    assert runner.resets == ["/tmp/docs"]
    assert orchestrator._current_project.name == "docs"
    await orchestrator.handle_message("do something", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert runner.turns[0][0] == "/tmp/docs"


async def test_verbose_mode_pushes_tool_events():
    runner = FakeRunner()
    runner.script = [ToolEvent("Bash", "go test ./..."), TextEvent("final result")]
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("/verbose on", AUTHORIZED)
    await orchestrator.handle_message("run tests", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert any("go test ./..." in m for m in sent)


async def test_brief_mode_suppresses_tool_events():
    runner = FakeRunner()
    runner.script = [ToolEvent("Bash", "go test ./..."), TextEvent("final result")]
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("run tests", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert not any("go test ./..." in m for m in sent)
    assert any("final result" in m for m in sent)


async def test_stop_with_no_task():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/stop", AUTHORIZED)
    assert any("No task" in m for m in sent)


async def test_permission_allow_does_not_escalate():
    orchestrator, runner, sent = build()
    approved = await orchestrator.request_permission(
        "Read", {"file_path": "/tmp/multica/a.py"}, "/tmp/multica"
    )
    assert approved is True
    assert not any("Permission needed" in m for m in sent)


async def test_permission_escalates_and_resolves_on_approve():
    orchestrator, runner, sent = build()

    async def approve_soon():
        await asyncio.sleep(0)
        await orchestrator.handle_message("ok", AUTHORIZED)

    result, _ = await asyncio.gather(
        orchestrator.request_permission("Bash", {"command": "rm x"}, "/tmp/multica"),
        approve_soon(),
    )
    assert result is True
    assert any("Permission needed" in m for m in sent)


async def test_permission_escalates_and_resolves_on_deny():
    orchestrator, runner, sent = build()

    async def deny_soon():
        await asyncio.sleep(0)
        await orchestrator.handle_message("no", AUTHORIZED)

    result, _ = await asyncio.gather(
        orchestrator.request_permission("Bash", {"command": "rm x"}, "/tmp/multica"),
        deny_soon(),
    )
    assert result is False


async def test_permission_timeout_denies():
    orchestrator, runner, sent = build()
    orchestrator._config.permission_timeout_seconds = 0
    result = await orchestrator.request_permission(
        "Bash", {"command": "rm x"}, "/tmp/multica"
    )
    assert result is False
    assert any("timed out" in m for m in sent)


async def test_concurrent_escalations_are_serialized():
    orchestrator, runner, sent = build()

    async def approve_twice():
        # resolve the first request, then the second
        for _ in range(2):
            while orchestrator._permission_future is None or orchestrator._permission_future.done():
                await asyncio.sleep(0)
            await orchestrator.handle_message("ok", AUTHORIZED)
            await asyncio.sleep(0)

    r1, r2, _ = await asyncio.gather(
        orchestrator.request_permission("Bash", {"command": "rm a"}, "/tmp/multica"),
        orchestrator.request_permission("Bash", {"command": "rm b"}, "/tmp/multica"),
        approve_twice(),
    )
    assert r1 is True and r2 is True
    # both escalation prompts were sent (not clobbered)
    assert sum("Permission needed" in m for m in sent) == 2


async def test_dry_run_echoes_without_running_turn():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/debug on", AUTHORIZED)
    await orchestrator.handle_message("fix the login bug", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert runner.turns == []
    assert any("Debug mode" in m and "fix the login bug" in m for m in sent)


async def test_dry_run_off_restores_normal_turn():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/debug on", AUTHORIZED)
    await orchestrator.handle_message("/debug off", AUTHORIZED)
    await orchestrator.handle_message("do work", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert runner.turns == [("/tmp/multica", "do work")]


async def test_dry_run_drains_queue():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/debug on", AUTHORIZED)
    orchestrator._task = asyncio.create_task(orchestrator._run("first"))
    await orchestrator.handle_message("second", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert sum("Echo:" in m for m in sent) == 2


async def test_geo_failure_skips_turn():
    async def geo_check():
        from claude_dingtalk_bridge.geo import GeoCheck
        return GeoCheck(ok=False, detail="📍 IP: 45.8.1.1\n❌ IP location: HK (expected: US)")

    orchestrator, runner, sent = build(geo_check=geo_check)
    await orchestrator.handle_message("do work", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert runner.turns == []
    assert any("skipped" in m and "HK" in m for m in sent)


async def test_geo_pass_runs_turn():
    async def geo_check():
        from claude_dingtalk_bridge.geo import GeoCheck
        return GeoCheck(ok=True, detail="✅ IP location verified: US")

    orchestrator, runner, sent = build(geo_check=geo_check)
    await orchestrator.handle_message("do work", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert runner.turns == [("/tmp/multica", "do work")]


async def test_geo_runs_before_dry_run_shortcut():
    """dry-run on: geo still runs; a geo failure beats the echo."""
    async def geo_check():
        from claude_dingtalk_bridge.geo import GeoCheck
        return GeoCheck(ok=False, detail="❌ Connect to the VPN first.")

    orchestrator, runner, sent = build(geo_check=geo_check)
    await orchestrator.handle_message("/debug on", AUTHORIZED)
    await orchestrator.handle_message("do work", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert runner.turns == []
    assert any("skipped" in m for m in sent)
    assert not any("Debug mode" in m and "do work" in m for m in sent)


async def test_status_shows_dry_run_mode():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/debug on", AUTHORIZED)
    await orchestrator.handle_message("/status", AUTHORIZED)
    assert any("**Debug:** on" in m for m in sent)


async def test_clear_interrupts_resets_and_drops_queue():
    orchestrator, runner, sent = build()
    orchestrator._task = asyncio.create_task(asyncio.sleep(100))
    orchestrator._queue = ["queued work"]
    await orchestrator.handle_message("/clear", AUTHORIZED)
    assert runner.interrupts == 1
    assert runner.resets == ["/tmp/multica"]
    assert orchestrator._queue == []
    assert any("reset the session" in m for m in sent)
    orchestrator._task.cancel()


async def test_help_lists_commands():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/help", AUTHORIZED)
    assert sent and "/debug" in sent[0] and "/clear" in sent[0]


async def test_unknown_command_replies_and_runs_no_turn():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/statu", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert runner.turns == []
    assert any("Unknown command" in m for m in sent)


async def test_cd_without_arg_shows_usage():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/cd", AUTHORIZED)
    assert any("Usage" in m for m in sent)


async def test_geo_pass_note_folded_into_start_message():
    async def geo_check():
        from claude_dingtalk_bridge.geo import GeoCheck
        return GeoCheck(ok=True, detail="📍 IP: 1.2.3.4\n✅ IP location verified: US")

    orchestrator, runner, sent = build(geo_check=geo_check)
    await orchestrator.handle_message("do work", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert any("Task started" in m and "US" in m for m in sent)


async def test_pwd_shows_current_project():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/pwd", AUTHORIZED)
    assert any("multica" in m and "/tmp/multica" in m for m in sent)


async def test_ls_shows_project_paths():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/ls", AUTHORIZED)
    assert any("/tmp/multica" in m and "/tmp/docs" in m for m in sent)


async def test_debug_without_arg_reports_state_without_changing_it():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/debug", AUTHORIZED)
    assert orchestrator._dry_run is False
    assert any("Debug mode is" in m for m in sent)


async def test_verbose_without_arg_reports_state():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/verbose", AUTHORIZED)
    assert orchestrator._verbose is False
    assert any("Verbose mode is" in m for m in sent)


_DB_QUESTION = {
    "questions": [
        {
            "question": "Which database?",
            "header": "Database",
            "multiSelect": False,
            "options": [
                {"label": "Postgres", "description": "Relational"},
                {"label": "SQLite", "description": "Embedded"},
            ],
        }
    ]
}


async def _reply_when_pending(orchestrator, reply):
    while orchestrator._question_future is None or orchestrator._question_future.done():
        await asyncio.sleep(0)
    await orchestrator.handle_message(reply, AUTHORIZED)


async def test_askuserquestion_number_maps_to_label():
    orchestrator, runner, sent = build()
    answer, _ = await asyncio.gather(
        orchestrator.answer_question(_DB_QUESTION, "/tmp/multica"),
        _reply_when_pending(orchestrator, "1"),
    )
    assert any("❓ Claude is asking" in m and "1. Postgres" in m for m in sent)
    assert "Database: Postgres" in answer
    assert "answered your AskUserQuestion" in answer


async def test_askuserquestion_free_text_answer():
    orchestrator, runner, sent = build()
    answer, _ = await asyncio.gather(
        orchestrator.answer_question(_DB_QUESTION, "/tmp/multica"),
        _reply_when_pending(orchestrator, "use DynamoDB"),
    )
    assert "Database: use DynamoDB" in answer


async def test_askuserquestion_out_of_range_reasks():
    orchestrator, runner, sent = build()

    async def reply_twice():
        await _reply_when_pending(orchestrator, "9")
        await _reply_when_pending(orchestrator, "2")

    answer, _ = await asyncio.gather(
        orchestrator.answer_question(_DB_QUESTION, "/tmp/multica"),
        reply_twice(),
    )
    assert any("out of range" in m for m in sent)
    assert "Database: SQLite" in answer


async def test_askuserquestion_multiple_questions():
    two = {
        "questions": [
            {
                "question": "Q1?", "header": "First", "multiSelect": False,
                "options": [{"label": "A", "description": ""}],
            },
            {
                "question": "Q2?", "header": "Second", "multiSelect": False,
                "options": [{"label": "B", "description": ""}],
            },
        ]
    }
    orchestrator, runner, sent = build()

    async def reply_twice():
        await _reply_when_pending(orchestrator, "1")
        await _reply_when_pending(orchestrator, "1")

    answer, _ = await asyncio.gather(
        orchestrator.answer_question(two, "/tmp/multica"),
        reply_twice(),
    )
    assert "First: A" in answer and "Second: B" in answer
    assert any("(1/2)" in m for m in sent) and any("(2/2)" in m for m in sent)


async def test_askuserquestion_timeout():
    orchestrator, runner, sent = build()
    orchestrator._config.permission_timeout_seconds = 0
    answer = await orchestrator.answer_question(_DB_QUESTION, "/tmp/multica")
    assert "did not answer" in answer
    assert any("timed out" in m for m in sent)


async def test_askuserquestion_keyword_reply_is_forwarded():
    """A bare 'no' while a question is pending is the answer, not a permission reply."""
    yes_no = {
        "questions": [
            {
                "question": "Proceed?", "header": "Confirm", "multiSelect": False,
                "options": [
                    {"label": "Yes", "description": ""},
                    {"label": "No", "description": ""},
                ],
            }
        ]
    }
    orchestrator, runner, sent = build()
    answer, _ = await asyncio.gather(
        orchestrator.answer_question(yes_no, "/tmp/multica"),
        _reply_when_pending(orchestrator, "no"),
    )
    assert "Confirm: no" in answer
    assert not any("No pending operation" in m for m in sent)


async def test_askuserquestion_stop_cancels():
    orchestrator, runner, sent = build()

    async def stop_when_pending():
        while orchestrator._question_future is None or orchestrator._question_future.done():
            await asyncio.sleep(0)
        await orchestrator.handle_message("/stop", AUTHORIZED)

    answer, _ = await asyncio.gather(
        orchestrator.answer_question(_DB_QUESTION, "/tmp/multica"),
        stop_when_pending(),
    )
    assert "did not answer" in answer


async def test_session_command_without_session():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/session", AUTHORIZED)
    assert any("No session yet" in m for m in sent)


async def test_session_command_with_session():
    orchestrator, runner, sent = build()
    sid = "550e8400-e29b-41d4-a716-446655440000"
    runner.set_session("/tmp/multica", sid)
    await orchestrator.handle_message("/session", AUTHORIZED)
    msg = next(m for m in sent if sid in m)
    # Layout: bold labels with id and transcript path each in their own fenced
    # code block — clients render a copy button on the block for one-tap copy.
    # ``$HOME`` is collapsed to ``~`` in the transcript path.
    assert "**Session ID:**" in msg
    assert f"```\n{sid}\n```" in msg
    assert "**Transcript:**" in msg
    assert "~/.claude/projects/" in msg
    assert f"{sid}.jsonl" in msg
    # No absolute home prefix should leak through.
    assert "/Users/" not in msg and str(Path.home()) not in msg


_UUID_A = "aaaaaaaa-0000-0000-0000-000000000000"
_UUID_B = "bbbbbbbb-0000-0000-0000-000000000000"


async def test_resume_lists_sessions(monkeypatch):
    orchestrator, runner, sent = build()

    async def fake_list(project_path, limit):
        return [FakeInfo(_UUID_A, "fix login"), FakeInfo(_UUID_B, "write docs")]

    monkeypatch.setattr(orch_mod, "list_recent_sessions", fake_list)
    await orchestrator.handle_message("/resume", AUTHORIZED)
    assert any("Recent sessions" in m for m in sent)
    assert orchestrator._resume_candidates == [_UUID_A, _UUID_B]


async def test_resume_empty_list(monkeypatch):
    orchestrator, runner, sent = build()

    async def fake_list(project_path, limit):
        return []

    monkeypatch.setattr(orch_mod, "list_recent_sessions", fake_list)
    await orchestrator.handle_message("/resume", AUTHORIZED)
    assert any("No past sessions" in m for m in sent)


async def test_resume_by_number(monkeypatch):
    orchestrator, runner, sent = build()
    orchestrator._resume_candidates = [_UUID_A, _UUID_B]
    await orchestrator.handle_message("/resume 2", AUTHORIZED)
    assert runner.current_session("/tmp/multica") == _UUID_B
    assert orchestrator._resume_candidates == []
    assert any("Resumed session" in m for m in sent)


async def test_resume_number_out_of_range():
    orchestrator, runner, sent = build()
    orchestrator._resume_candidates = [_UUID_A]
    await orchestrator.handle_message("/resume 5", AUTHORIZED)
    assert runner.current_session("/tmp/multica") is None
    assert any("1-1" in m for m in sent)


async def test_resume_number_zero_rejected():
    orchestrator, runner, sent = build()
    orchestrator._resume_candidates = [_UUID_A]
    await orchestrator.handle_message("/resume 0", AUTHORIZED)
    assert runner.current_session("/tmp/multica") is None
    assert any("1-1" in m for m in sent)


async def test_resume_number_without_listing():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/resume 1", AUTHORIZED)
    assert any("`/resume` first" in m for m in sent)


async def test_resume_by_uuid(monkeypatch):
    orchestrator, runner, sent = build()

    async def fake_find(project_path, session_id):
        return FakeInfo(session_id)

    monkeypatch.setattr(orch_mod, "find_session", fake_find)
    await orchestrator.handle_message(f"/resume {_UUID_A}", AUTHORIZED)
    assert runner.current_session("/tmp/multica") == _UUID_A


async def test_resume_uuid_not_in_project(monkeypatch):
    orchestrator, runner, sent = build()

    async def fake_find(project_path, session_id):
        return None

    monkeypatch.setattr(orch_mod, "find_session", fake_find)
    await orchestrator.handle_message(f"/resume {_UUID_A}", AUTHORIZED)
    assert runner.current_session("/tmp/multica") is None
    assert any("not found" in m for m in sent)


async def test_resume_rejected_while_running():
    orchestrator, runner, sent = build()

    async def block():
        await asyncio.sleep(0.05)

    orchestrator._task = asyncio.create_task(block())
    await orchestrator.handle_message("/resume 1", AUTHORIZED)
    assert runner.current_session("/tmp/multica") is None
    assert any("task is running" in m.lower() for m in sent)
    await orchestrator._task


async def test_switch_project_clears_resume_candidates():
    orchestrator, runner, sent = build()
    orchestrator._resume_candidates = [_UUID_A]
    await orchestrator.handle_message("/cd docs", AUTHORIZED)
    assert orchestrator._resume_candidates == []


async def test_resume_garbage_arg():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/resume notauuid", AUTHORIZED)
    assert runner.current_session("/tmp/multica") is None
    assert any("Usage" in m for m in sent)


async def test_done_message_goes_to_markdown_channel():
    runner = FakeRunner()
    runner.script = [TextEvent("**bold** result")]
    orchestrator, runner, text_sent, md_sent = build_channels(runner)
    await orchestrator.handle_message("do it", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert any("**bold** result" in m for m in md_sent)
    assert not any("**bold** result" in m for m in text_sent)


async def test_verbose_text_goes_to_markdown_channel():
    runner = FakeRunner()
    runner.script = [TextEvent("# heading"), TextEvent("final")]
    orchestrator, runner, text_sent, md_sent = build_channels(runner)
    await orchestrator.handle_message("/verbose on", AUTHORIZED)
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert any("# heading" in m for m in md_sent)


async def test_tool_events_and_status_stay_on_text_channel():
    runner = FakeRunner()
    runner.script = [ToolEvent("Bash", "ls"), TextEvent("done")]
    orchestrator, runner, text_sent, md_sent = build_channels(runner)
    await orchestrator.handle_message("/verbose on", AUTHORIZED)
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert any("Task started" in m for m in text_sent)
    assert any("🔧" in m for m in text_sent)
    assert not any("🔧" in m for m in md_sent)


def test_format_tokens_plain_below_1000():
    from claude_dingtalk_bridge.orchestrator import format_tokens

    assert format_tokens(0) == "0"
    assert format_tokens(999) == "999"


def test_format_tokens_thousands():
    from claude_dingtalk_bridge.orchestrator import format_tokens

    assert format_tokens(1000) == "1K"
    assert format_tokens(1200) == "1.2K"
    assert format_tokens(45000) == "45K"


def test_format_tokens_millions():
    from claude_dingtalk_bridge.orchestrator import format_tokens

    assert format_tokens(1_000_000) == "1M"
    assert format_tokens(1_500_000) == "1.5M"
    assert format_tokens(12_300_000) == "12.3M"


async def test_status_with_no_turns_omits_session_tokens():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/status", AUTHORIZED)
    msg = "\n".join(sent)
    assert "Session tokens" not in msg
    assert "Cache" not in msg


async def test_status_shows_session_tokens_and_cache():
    runner = FakeRunner()
    runner.token_totals["/tmp/multica"] = 1_200_000
    runner.usages["/tmp/multica"] = {
        "cache_read_input_tokens": 45000,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 8000,
            "ephemeral_5m_input_tokens": 0,
        },
    }
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("/status", AUTHORIZED)
    msg = "\n".join(sent)
    assert "**Session tokens:** 1.2M" in msg
    assert "**Cache last turn:** read 45K (hit 84.9%) · creation 8K" in msg


async def test_status_shows_per_model_breakdown():
    runner = FakeRunner()
    runner.token_totals["/tmp/multica"] = 1_200_000
    runner.model_token_totals["/tmp/multica"] = {
        "claude-opus-4-7[1m]": 900_000,
        "claude-haiku-4-5-20251001": 300_000,
    }
    runner.usages["/tmp/multica"] = {
        "cache_read_input_tokens": 45000,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 8000,
            "ephemeral_5m_input_tokens": 0,
        },
    }
    runner.model_usages["/tmp/multica"] = {
        "claude-opus-4-7[1m]": {
            "inputTokens": 100, "outputTokens": 1500,
            "cacheReadInputTokens": 45000, "cacheCreationInputTokens": 7000,
        },
        "claude-haiku-4-5-20251001": {
            "inputTokens": 50, "outputTokens": 200,
            "cacheReadInputTokens": 0, "cacheCreationInputTokens": 1000,
        },
    }
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("/status", AUTHORIZED)
    msg = "\n".join(sent)
    assert "**Session tokens:** 1.2M" in msg
    assert "  - opus-4.7[1m]: 900K" in msg
    assert "  - haiku-4.5: 300K" in msg
    assert "**Cache last turn:** read 45K (hit 84.7%) · creation 8K" in msg
    assert "  - opus-4.7[1m]: read 45K (hit 86.4%) · creation 7K" in msg
    assert "  - haiku-4.5: read 0 (hit 0.0%) · creation 1K" in msg


async def test_status_single_model_omits_breakdown():
    runner = FakeRunner()
    runner.token_totals["/tmp/multica"] = 1000
    runner.model_token_totals["/tmp/multica"] = {"claude-opus-4-7": 1000}
    runner.usages["/tmp/multica"] = {"cache_read_input_tokens": 100}
    runner.model_usages["/tmp/multica"] = {
        "claude-opus-4-7": {
            "inputTokens": 5, "outputTokens": 5,
            "cacheReadInputTokens": 100, "cacheCreationInputTokens": 50,
        },
    }
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("/status", AUTHORIZED)
    msg = "\n".join(sent)
    assert "  - opus-4.7" not in msg


async def test_model_no_arg_lists_models():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/model", AUTHORIZED)
    msg = "\n".join(sent)
    assert "Models" in msg
    assert "sonnet" in msg


async def test_model_switch_by_number():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/model 3", AUTHORIZED)
    assert runner.model_override == "sonnet"


async def test_model_switch_by_name():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/model opus", AUTHORIZED)
    assert runner.model_override == "opus"


async def test_model_number_out_of_range():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/model 9", AUTHORIZED)
    assert runner.model_override is None
    assert any("1-4" in m for m in sent)


async def test_model_list_marks_current():
    orchestrator, runner, sent = build()
    runner.model_override = "sonnet"
    await orchestrator.handle_message("/model", AUTHORIZED)
    assert any("(current)" in m for m in sent)


async def test_status_shows_model():
    orchestrator, runner, sent = build()
    runner.model_override = "haiku"
    await orchestrator.handle_message("/status", AUTHORIZED)
    assert any("**Model:** haiku" in m for m in sent)


async def test_status_shows_default_model_when_unset():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/status", AUTHORIZED)
    assert any("**Model:** default" in m for m in sent)


async def test_help_lists_new_commands():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/help", AUTHORIZED)
    text = "\n".join(sent)
    assert "/compact" in text
    assert "/model" in text


async def test_todo_event_pushes_rendered_checklist():
    from claude_dingtalk_bridge.claude_runner import TodoEvent

    runner = FakeRunner()
    runner.script = [
        TodoEvent([
            ("Fix bug", "completed", "Fixing bug"),
            ("Add tests", "in_progress", "Adding tests"),
            ("Update docs", "pending", "Updating docs"),
        ]),
        TextEvent("all set"),
    ]
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    checklist = next(m for m in sent if "Tasks" in m)
    assert "(1/3)" in checklist
    assert "~~Fix bug~~" in checklist
    assert "✍︎ **Adding tests**" in checklist
    assert "☕︎ Update docs" in checklist


async def test_todo_event_dedups_identical_snapshots():
    from claude_dingtalk_bridge.claude_runner import TodoEvent

    snapshot = [("A", "pending", "Doing A")]
    runner = FakeRunner()
    runner.script = [TodoEvent(snapshot), TodoEvent(list(snapshot)), TextEvent("done")]
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert sum("Tasks" in m for m in sent) == 1


async def test_turn_reply_sent_without_done_header():
    runner = FakeRunner()
    runner.script = [TextEvent("here is the answer")]
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert any(m == "here is the answer" for m in sent)
    assert not any("Done" in m for m in sent)


async def test_every_text_block_is_sent_not_just_the_last():
    # A turn that narrates several results produces several text blocks —
    # all must reach the phone, not only the last one.
    runner = FakeRunner()
    runner.script = [
        TextEvent("Agent A answer"),
        TextEvent("Agent B answer"),
    ]
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert any(m == "Agent A answer" for m in sent)
    assert any(m == "Agent B answer" for m in sent)


async def test_pending_background_agent_adds_a_running_notice(monkeypatch):
    from claude_dingtalk_bridge.claude_runner import TaskEvent

    # Shrink the delay so the test doesn't wait 30 seconds for the notice.
    monkeypatch.setattr(orch_mod, "_PENDING_NOTICE_DELAY", 0.01)
    runner = FakeRunner()
    runner.script = [
        TaskEvent("started", "task-1", description="Fix Task 1"),
        TextEvent("kicked off"),
    ]
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    # The notice is now delayed; wait for the timer task to finish before
    # asserting it landed.
    if orchestrator._pending_notice_task is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await orchestrator._pending_notice_task
    assert any("🚀 Subagent started · Fix Task 1" in m for m in sent)
    assert any("1 background agent still running" in m for m in sent)


async def test_pending_notice_suppressed_when_subagent_finishes_before_delay(
    monkeypatch,
):
    # The whole point of the delay: a subagent that completes within the
    # window cancels the pending-notice timer, so the phone never sees the
    # "still running" line.
    from claude_dingtalk_bridge.claude_runner import TaskEvent

    monkeypatch.setattr(orch_mod, "_PENDING_NOTICE_DELAY", 0.05)
    runner = FakeRunner()
    started = TaskEvent("started", "task-1", description="Fix Task 1")
    notification = TaskEvent(
        "notification",
        "task-1",
        description="Fix Task 1",
        status="completed",
        summary="Task 1 fixed",
        duration_ms=1000,
        total_tokens=4242,
    )

    async def run_turn(project_path, prompt, emit):
        runner.turns.append((project_path, prompt))
        await emit(started)
        await emit(TextEvent("kicked off"))
        await emit(ResultEvent("", False))
        # Notification arrives after the turn ends but before the delay
        # elapses — this is the race the delay is designed to cover.
        await emit(notification)

    runner.run_turn = run_turn
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    # Let the (now-cancelled) timer's sleep window pass to be sure nothing
    # leaks through later.
    await asyncio.sleep(0.1)
    assert any("✅ Task 1 fixed" in m for m in sent)
    assert not any("background agent still running" in m for m in sent)


async def test_synchronous_subagent_adds_no_running_notice():
    from claude_dingtalk_bridge.claude_runner import TaskEvent

    runner = FakeRunner()
    runner.script = [
        TaskEvent("started", "task-1", description="Fix Task 1"),
        TaskEvent(
            "notification",
            "task-1",
            description="Fix Task 1",
            status="completed",
            summary="Task 1 fixed",
            duration_ms=3000,
            total_tokens=16641,
        ),
        TextEvent("done"),
    ]
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    # Completion line uses only `summary` to avoid double-printing the
    # description (the SDK's summary already names the task).
    assert any("✅ Task 1 fixed (3.0s, 16.6K)" in m for m in sent)
    assert not any("background agent still running" in m for m in sent)


async def test_subagent_notification_without_usage_omits_suffix():
    # SDK may leave duration_ms/total_tokens at 0 on abnormal stops; the line
    # should drop the parenthetical rather than showing '(0.0s, 0)'.
    from claude_dingtalk_bridge.claude_runner import TaskEvent

    runner = FakeRunner()
    runner.script = [
        TaskEvent("started", "t1", description="Quick task"),
        TaskEvent(
            "notification",
            "t1",
            description="Quick task",
            status="stopped",
        ),
        TextEvent("done"),
    ]
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    # Falls back to description when SDK summary is empty (status=stopped).
    assert any(m == "⏹ Quick task" for m in sent)


async def test_task_progress_event_never_pushes_to_phone():
    # Progress events still drive _track_pending bookkeeping, but in either
    # verbose or brief mode they produce no phone message — the subagent's
    # ToolEvents already convey the same activity with richer detail.
    from claude_dingtalk_bridge.claude_runner import TaskEvent

    for mode_prefix in ([], ["/verbose on"]):
        runner = FakeRunner()
        runner.script = [
            TaskEvent("progress", "task-1", description="Fix Task 1", last_tool="Edit"),
            TextEvent("done"),
        ]
        orchestrator, runner, sent = build(runner)
        for cmd in mode_prefix:
            await orchestrator.handle_message(cmd, AUTHORIZED)
        await orchestrator.handle_message("go", AUTHORIZED)
        await _wait_idle(orchestrator)
        assert not any("Fix Task 1" in m for m in sent)


async def test_colon_terminated_text_dropped_in_non_verbose_mode():
    # A text block that ends with `:` or `：` is pre-tool narration — drop
    # it; period/exclamation/question-terminated blocks are real content.
    runner = FakeRunner()
    runner.script = [
        TextEvent("Now I'll list files:"),
        TextEvent("现在查一下文件："),
        TextEvent("Here's the final answer."),
        TextEvent("看看代码就知道了。"),
    ]
    orchestrator, runner, sent = build(runner)
    assert orchestrator._verbose is False
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert not any("Now I'll list files" in m for m in sent)
    assert not any("现在查一下文件" in m for m in sent)
    assert any("final answer" in m for m in sent)
    assert any("看看代码就知道了" in m for m in sent)


async def test_markdown_bolded_colon_narration_still_dropped():
    # Regression: ``**Now I'll do X:**`` rstrips to ``**`` (not ``:``), so the
    # old check missed bolded narration and leaked it to the phone. The fix
    # strips trailing markdown emphasis (`*`/`_`) before the colon check.
    runner = FakeRunner()
    runner.script = [
        TextEvent("**Now implementing all 7 changes:**"),
        TextEvent("*next step:*"),
        TextEvent("__然后看一下：__"),
        TextEvent("**Here's the final answer.**"),
    ]
    orchestrator, runner, sent = build(runner)
    assert orchestrator._verbose is False
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert not any("Now implementing" in m for m in sent)
    assert not any("next step" in m for m in sent)
    assert not any("然后看一下" in m for m in sent)
    assert any("final answer" in m for m in sent)


async def test_colon_terminated_text_kept_in_verbose_mode():
    # Verbose mode bypasses the narration filter — useful for debugging.
    runner = FakeRunner()
    runner.script = [
        TextEvent("Now I'll list files:"),
        TextEvent("Here's the final answer."),
    ]
    orchestrator, runner, sent = build(runner)
    orchestrator._verbose = True
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert any("Now I'll list files" in m for m in sent)
    assert any("final answer" in m for m in sent)


async def test_result_event_fallback_fires_when_all_text_was_narration():
    # If every TextEvent was filtered as narration, _turn_sent_text stays
    # False; the ResultEvent fallback then surfaces the SDK's final result so
    # the user still sees an answer.
    runner = FakeRunner()
    runner.script = [
        TextEvent("Working on it:"),
        ResultEvent("Here is the actual answer.", False),
    ]
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert any("Here is the actual answer" in m for m in sent)


async def test_emit_drops_events_after_a_turn_is_cancelled():
    # A stopped/cleared turn keeps unwinding; _emit must swallow whatever it
    # still produces (e.g. a stale background-drain timeout).
    orchestrator, runner, sent = build()
    orchestrator._turn_cancelled = True
    await orchestrator._emit(TextEvent("stale reply"))
    await orchestrator._emit(ResultEvent("late", False))
    assert sent == []


async def test_clear_cancels_an_in_flight_turn():
    # /clear must actually cancel the running task — interrupt() alone never
    # ends a lingering background drain.
    runner = FakeRunner()
    gate = asyncio.Event()

    async def blocking_run_turn(project_path, prompt, emit):
        await gate.wait()

    runner.run_turn = blocking_run_turn
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    for _ in range(5):
        await asyncio.sleep(0)
    task = orchestrator._task
    assert task is not None and not task.done()

    await orchestrator.handle_message("/clear", AUTHORIZED)
    assert orchestrator._turn_cancelled is True
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()
    gate.set()


async def test_stop_confirms_session_is_kept():
    # /stop should reassure the phone that the session survives the interrupt.
    runner = FakeRunner()
    gate = asyncio.Event()

    async def blocking_run_turn(project_path, prompt, emit):
        await gate.wait()

    runner.run_turn = blocking_run_turn
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    for _ in range(5):
        await asyncio.sleep(0)
    assert orchestrator._task is not None

    await orchestrator.handle_message("/stop", AUTHORIZED)
    assert orchestrator._task is None
    assert any("Task stopped" in m and "go on" in m for m in sent)
    gate.set()


# --- coverage of remaining branches ------------------------------------

def test_display_path_renders_home_as_tilde():
    from pathlib import Path

    assert orch_mod.display_path(str(Path.home())) == "~"


def test_summary_truncates_long_text():
    summary = orch_mod._summary("x" * 200)
    assert summary.endswith("…")
    assert len(summary) == orch_mod._PROMPT_SUMMARY_LIMIT + 1


async def test_stop_denies_a_pending_permission():
    orchestrator, runner, sent = build()
    loop = asyncio.get_running_loop()
    orchestrator._permission_future = loop.create_future()
    await orchestrator.handle_message("/stop", AUTHORIZED)
    assert orchestrator._permission_future.result() is False
    assert any("Denied the pending operation" in m for m in sent)


async def test_permission_reply_without_pending_request():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("ok", AUTHORIZED)
    assert any("No pending operation to confirm" in m for m in sent)


async def test_verbose_off_disables_verbose():
    orchestrator, runner, sent = build()
    orchestrator._verbose = True
    await orchestrator.handle_message("/verbose off", AUTHORIZED)
    assert orchestrator._verbose is False
    assert any("Verbose mode off" in m for m in sent)


async def test_switch_project_blocked_while_task_running():
    runner = FakeRunner()
    gate = asyncio.Event()

    async def blocking_run_turn(project_path, prompt, emit):
        await gate.wait()

    runner.run_turn = blocking_run_turn
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    for _ in range(5):
        await asyncio.sleep(0)
    await orchestrator.handle_message("/cd docs", AUTHORIZED)
    assert orchestrator._current_project.name == "multica"
    assert any("A task is running" in m for m in sent)
    gate.set()
    await _wait_idle(orchestrator)


async def test_status_reports_queue_depth():
    runner = FakeRunner()
    gate = asyncio.Event()

    async def blocking_run_turn(project_path, prompt, emit):
        await gate.wait()

    runner.run_turn = blocking_run_turn
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("first", AUTHORIZED)
    for _ in range(5):
        await asyncio.sleep(0)
    await orchestrator.handle_message("second", AUTHORIZED)
    await orchestrator.handle_message("/status", AUTHORIZED)
    assert any("queue: 1" in m for m in sent)
    gate.set()
    await _wait_idle(orchestrator)


async def test_model_list_shows_external_override():
    runner = FakeRunner()
    runner.model_override = "claude-custom-id-99"
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("/model", AUTHORIZED)
    assert any("set via /model" in m for m in sent)


async def test_model_list_shows_observed_default():
    runner = FakeRunner()
    runner.model_override = None
    runner.observed_model = "claude-opus-4-7"
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("/model", AUTHORIZED)
    assert any("claude-opus-4-7" in m for m in sent)


async def test_empty_prompt_is_ignored():
    orchestrator, runner, sent = build()
    await orchestrator._cmd_prompt("")
    assert runner.turns == []
    assert orchestrator._task is None


async def test_run_surfaces_a_failed_turn():
    runner = FakeRunner()

    async def failing_run_turn(project_path, prompt, emit):
        raise RuntimeError("turn blew up")

    runner.run_turn = failing_run_turn
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert any("Task failed" in m and "turn blew up" in m for m in sent)


async def test_new_prompt_preempts_drain_phase_without_queueing():
    # While the runner is sitting in _drain_background waiting for a missing
    # task_notification, a fresh prompt should cancel the drain and start
    # immediately — not silently sit behind the stale turn.
    drain_release = asyncio.Event()

    class DrainingRunner(FakeRunner):
        async def run_turn(self, project_path, prompt, emit):
            self.turns.append((project_path, prompt))
            self.is_draining = True
            try:
                await drain_release.wait()
            finally:
                self.is_draining = False

        def cancel_drain(self) -> None:
            super().cancel_drain()
            drain_release.set()

    runner = DrainingRunner()
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("first", AUTHORIZED)
    # Spin until the first turn has entered the (fake) drain phase.
    for _ in range(50):
        if runner.is_draining:
            break
        await asyncio.sleep(0)
    assert runner.is_draining

    await orchestrator.handle_message("second", AUTHORIZED)
    await _wait_idle(orchestrator)

    assert runner.drain_cancels == 1
    # The new prompt ran without ever being queued.
    assert not any("queued" in m for m in sent)
    assert runner.turns == [("/tmp/multica", "first"), ("/tmp/multica", "second")]


async def test_task_timeout_event_notifies_phone():
    from claude_dingtalk_bridge.claude_runner import TaskEvent

    runner = FakeRunner()
    runner.script = [
        TaskEvent("started", "task-1", description="Fix Task 1"),
        TaskEvent("timeout", "task-1"),
        TextEvent("done"),
    ]
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert any("background agent hasn't reported back" in m for m in sent)


async def test_acknowledged_subagent_suppresses_pending_notice(monkeypatch):
    # An acknowledged event (SDK marked the task done via task_updated, but
    # no task_notification arrived) must NOT count toward the "still
    # running" notice — the only reason that notice exists is to warn about
    # subagents the SDK doesn't know are done yet.
    from claude_dingtalk_bridge.claude_runner import TaskEvent

    monkeypatch.setattr(orch_mod, "_PENDING_NOTICE_DELAY", 0.01)
    runner = FakeRunner()
    runner.script = [
        TaskEvent("started", "task-1", description="Fix Task 1"),
        TaskEvent("acknowledged", "task-1", status="completed"),
        TextEvent("kicked off"),
    ]
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    # Give the would-be notice timer a chance to fire (it must not).
    await asyncio.sleep(0.05)
    assert not any("background agent still running" in m for m in sent)
    # The acknowledged event is bookkeeping only — never a phone message.
    assert not any("Fix Task 1" in m and "✅" in m for m in sent)


async def test_pending_notice_still_fires_for_unacknowledged_subagent(monkeypatch):
    # Sanity counterpart: a started task with no acknowledged event still
    # produces the "still running" warning after the delay — we only
    # suppress when the SDK has signalled completion.
    from claude_dingtalk_bridge.claude_runner import TaskEvent

    monkeypatch.setattr(orch_mod, "_PENDING_NOTICE_DELAY", 0.01)
    runner = FakeRunner()
    runner.script = [
        TaskEvent("started", "task-1", description="Slow Task"),
        TaskEvent("started", "task-2", description="Quick Task"),
        TaskEvent("acknowledged", "task-2", status="completed"),
        TextEvent("kicked off"),
    ]
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    if orchestrator._pending_notice_task is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await orchestrator._pending_notice_task
    # Only task-1 should count as still running — one task, singular noun.
    assert any("1 background agent still running" in m for m in sent)


async def test_answer_question_without_questions_returns_default():
    orchestrator, runner, sent = build()
    result = await orchestrator.answer_question({"questions": []}, "/tmp/multica")
    assert "No question was provided" in result
    assert sent == []


async def test_stop_skips_stopped_message_when_queued_prompt_takes_over():
    # A prompt queued behind the running task spins up as the next task the
    # moment /stop cancels the first — self._task is not None afterwards, so
    # the "Task stopped" confirmation is skipped (the takeover announced itself).
    runner = FakeRunner()
    gate_first = asyncio.Event()
    gate_second = asyncio.Event()
    started: list[str] = []

    async def run_turn(project_path, prompt, emit):
        started.append(prompt)
        await (gate_first if prompt == "first" else gate_second).wait()

    runner.run_turn = run_turn
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("first", AUTHORIZED)
    for _ in range(5):
        await asyncio.sleep(0)
    await orchestrator.handle_message("second", AUTHORIZED)
    await orchestrator.handle_message("/stop", AUTHORIZED)
    # The queued "second" is now the live task — no stop confirmation went out.
    assert not any("Task stopped" in m for m in sent)
    gate_second.set()
    await _wait_idle(orchestrator)
    assert "second" in started


async def test_running_turn_log_includes_prompt_preview(caplog):
    import logging
    orchestrator, _, _ = build()
    with caplog.at_level(logging.INFO, logger="claude_dingtalk_bridge.orchestrator"):
        await orchestrator.handle_message(
            "fix the cache invalidation bug in handler.py", AUTHORIZED,
        )
        await _wait_idle(orchestrator)
    lines = [r.getMessage() for r in caplog.records if "Running turn" in r.getMessage()]
    assert lines, "no Running turn line emitted"
    # Turn number is embedded in the banner itself — the line is logged BEFORE
    # log_context is stamped, so the formatter's `session=… turn=…` column is
    # blank for this single line and the count would otherwise be invisible.
    assert lines[0].startswith("Running turn 1: ")
    assert 'prompt="fix the cache invalidation bug in handler.py"' in lines[0]
    assert "project=multica" in lines[0]


async def test_running_turn_log_truncates_long_prompt(caplog):
    # Long prompts must collapse newlines and truncate so the log stays
    # one-line-greppable rather than blowing up the entry.
    import logging
    orchestrator, _, _ = build()
    long_prompt = "do this " * 30 + "\nthen that"
    with caplog.at_level(logging.INFO, logger="claude_dingtalk_bridge.orchestrator"):
        await orchestrator.handle_message(long_prompt, AUTHORIZED)
        await _wait_idle(orchestrator)
    lines = [r.getMessage() for r in caplog.records if "Running turn" in r.getMessage()]
    assert "\n" not in lines[0]
    assert "…" in lines[0]


async def test_request_permission_logs_escalation_to_phone(caplog):
    # Auto-allow is DEBUG (common case, no need to flood INFO); escalations
    # and the eventual phone reply are INFO so an operator can trace what
    # was asked and how long it took.
    import logging
    orchestrator, _runner, sent = build()
    with caplog.at_level(logging.INFO, logger="claude_dingtalk_bridge.orchestrator"):
        task = asyncio.create_task(
            orchestrator.request_permission(
                "Bash", {"command": "rm -rf /tmp/x"}, "/tmp/multica",
            )
        )
        # Yield enough for the escalation message to be emitted before we reply.
        for _ in range(20):
            await asyncio.sleep(0)
            if orchestrator._permission_future is not None:
                break
        await orchestrator._cmd_permission_reply(True)
        result = await task
    assert result is True
    lines = [r.getMessage() for r in caplog.records]
    assert any(
        "permission escalate" in l and "tool=Bash" in l for l in lines
    )
    assert any(
        "permission reply" in l and "result=allowed" in l for l in lines
    )


async def test_request_permission_logs_denial(caplog):
    import logging
    orchestrator, _runner, _sent = build()
    with caplog.at_level(logging.INFO, logger="claude_dingtalk_bridge.orchestrator"):
        task = asyncio.create_task(
            orchestrator.request_permission(
                "Bash", {"command": "rm -rf /tmp/x"}, "/tmp/multica",
            )
        )
        for _ in range(20):
            await asyncio.sleep(0)
            if orchestrator._permission_future is not None:
                break
        await orchestrator._cmd_permission_reply(False)
        result = await task
    assert result is False
    lines = [r.getMessage() for r in caplog.records]
    assert any(
        "permission reply" in l and "result=denied" in l for l in lines
    )


async def test_request_permission_logs_timeout(caplog, monkeypatch):
    import logging
    orchestrator, _runner, _sent = build()
    # Squash the long default timeout so the test doesn't actually wait.
    monkeypatch.setattr(orchestrator._config, "permission_timeout_seconds", 0.05)
    with caplog.at_level(logging.INFO, logger="claude_dingtalk_bridge.orchestrator"):
        result = await orchestrator.request_permission(
            "Bash", {"command": "rm -rf /tmp/x"}, "/tmp/multica",
        )
    assert result is False
    lines = [r.getMessage() for r in caplog.records]
    assert any("permission timeout" in l for l in lines)


async def test_request_permission_auto_allow_at_debug_only(caplog):
    import logging
    orchestrator, _runner, _sent = build()
    # `git status` is on the allowed_bash list — should auto-allow silently.
    with caplog.at_level(logging.DEBUG, logger="claude_dingtalk_bridge.orchestrator"):
        result = await orchestrator.request_permission(
            "Bash", {"command": "git status"}, "/tmp/multica",
        )
    assert result is True
    # No INFO escalation/reply lines for the auto-allow path.
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert not any("permission" in l for l in info_lines)
    # The decision is still traceable at DEBUG.
    debug_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("permission auto-allow" in l and "tool=Bash" in l for l in debug_lines)


async def test_answer_question_logs_round_trip(caplog):
    # The question round-trip is the entire conversation between phone and
    # Claude during an AskUserQuestion — currently invisible in logs. Verify
    # the entry and exit are logged with the question count.
    import logging
    orchestrator, _runner, _sent = build()
    with caplog.at_level(logging.INFO, logger="claude_dingtalk_bridge.orchestrator"):
        answer, _ = await asyncio.gather(
            orchestrator.answer_question(_DB_QUESTION, "/tmp/multica"),
            _reply_when_pending(orchestrator, "1"),
        )
    lines = [r.getMessage() for r in caplog.records]
    assert any(
        "ask_user_question count=1" in l for l in lines
    ), f"no entry log: {lines}"
    assert any(
        "ask_user_question answered count=1" in l for l in lines
    ), f"no answered log: {lines}"


async def test_answer_question_logs_empty_questions(caplog):
    import logging
    orchestrator, _runner, _sent = build()
    with caplog.at_level(logging.INFO, logger="claude_dingtalk_bridge.orchestrator"):
        result = await orchestrator.answer_question({"questions": []}, "/tmp/multica")
    assert "No question was provided" in result
    lines = [r.getMessage() for r in caplog.records]
    assert any("ask_user_question empty" in l for l in lines)


# --- shutdown / notify -------------------------------------------------


async def test_notify_pushes_markdown_through_send_markdown_channel():
    orchestrator, _runner, _text, md_sent = build_channels()
    await orchestrator.notify("📷 image failed")
    assert md_sent == ["📷 image failed"]


async def test_shutdown_resolves_pending_permission_future():
    orchestrator, _runner, _sent = build()
    loop = asyncio.get_running_loop()
    orchestrator._permission_future = loop.create_future()
    await orchestrator.shutdown()
    assert orchestrator._permission_future.done()
    assert orchestrator._permission_future.result() is False


async def test_shutdown_resolves_pending_question_future():
    orchestrator, _runner, _sent = build()
    loop = asyncio.get_running_loop()
    orchestrator._question_future = loop.create_future()
    await orchestrator.shutdown()
    assert orchestrator._question_future.done()
    assert orchestrator._question_future.result() is None


async def test_shutdown_clears_queue_and_cancels_task():
    orchestrator, runner, _sent = build()
    # Stage a queued prompt and a fake running task — shutdown should drain
    # both and call interrupt on the runner.
    orchestrator._queue.extend(["queued one", "queued two"])

    async def sleeper():
        await asyncio.Event().wait()

    orchestrator._task = asyncio.create_task(sleeper())
    await asyncio.sleep(0)  # let the task start
    await orchestrator.shutdown()
    assert orchestrator._queue == []
    assert runner.interrupts == 1
    assert orchestrator._task.done()


async def test_shutdown_is_idempotent():
    orchestrator, _runner, _sent = build()
    await orchestrator.shutdown()
    await orchestrator.shutdown()  # second call must not raise


# --- _prompt_log_summary ----------------------------------------------------


def test_prompt_log_summary_passes_short_text_through():
    from claude_dingtalk_bridge.orchestrator import _prompt_log_summary
    out = _prompt_log_summary("fix the cache bug")
    assert out == "fix the cache bug"


def test_prompt_log_summary_collapses_newlines_in_short_text():
    from claude_dingtalk_bridge.orchestrator import _prompt_log_summary
    out = _prompt_log_summary("line one\nline two")
    assert out == "line one line two"


def test_prompt_log_summary_extends_to_next_sentence_when_first_is_short():
    # First sentence is far below SOFT (80) — keep extending until we land
    # on a boundary past SOFT instead of returning a 5-char snippet.
    from claude_dingtalk_bridge.orchestrator import _prompt_log_summary
    text = "Yes. " * 30  # each "Yes. " is 5 chars, hard cut would lose context
    out = _prompt_log_summary(text)
    # We extended past 80 to a sentence boundary, so the count of "Yes." > 1.
    assert out.count("Yes.") > 1


def test_prompt_log_summary_cuts_at_sentence_boundary_just_past_soft():
    # Long first sentence — should cut at the FIRST boundary at or past SOFT
    # (80), not chop mid-word like the old 80-char hard truncate.
    from claude_dingtalk_bridge.orchestrator import _prompt_log_summary
    s1 = "x" * 100  # 100 chars, no boundary
    text = s1 + ". next sentence."
    out = _prompt_log_summary(text)
    # Output ends at the period after the 100 x's (101 chars + …)
    assert out.endswith("…")
    assert "next sentence" not in out


def test_prompt_log_summary_hard_caps_at_300_when_no_boundary():
    # No periods, no newlines, just a wall of chars — hard-truncate at HARD.
    from claude_dingtalk_bridge.orchestrator import _prompt_log_summary
    text = "x" * 1000
    out = _prompt_log_summary(text)
    assert len(out) == 301  # 300 chars + "…"
    assert out.endswith("…")


def test_prompt_log_summary_no_boundary_under_hard_takes_full_text():
    # Length between SOFT (80) and HARD (300) with no sentence boundary: the
    # loop exhausts naturally (no break), `cut` stays None and falls back to
    # min(len(text), HARD) = len(text), so the whole text is returned with
    # no ellipsis.
    from claude_dingtalk_bridge.orchestrator import _prompt_log_summary
    text = "x" * 200  # 80 < 200 < 300, no boundary chars
    out = _prompt_log_summary(text)
    assert out == text
    assert not out.endswith("…")


def test_prompt_log_summary_handles_chinese_punctuation():
    from claude_dingtalk_bridge.orchestrator import _prompt_log_summary
    text = "短句一。" + "字" * 100 + "。后面还有"
    out = _prompt_log_summary(text)
    # First sentence is only 5 chars (well under SOFT) so we extend; the
    # full-width period at position ~106 is the boundary we land on.
    assert "短句一。" in out
    assert "后面" not in out  # boundary before reaching that


# --- /stop interrupt log line ----------------------------------------------


async def test_cmd_stop_logs_interrupt_when_task_is_running(caplog):
    import logging
    orchestrator, runner, _sent = build()
    # Inflate a "running task" so _cmd_stop hits the interrupt branch.
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def slow_turn(project_path, prompt, emit):
        started.set()
        await blocked.wait()

    runner.run_turn = slow_turn
    await orchestrator.handle_message("do work", AUTHORIZED)
    await started.wait()
    with caplog.at_level(logging.INFO, logger="claude_dingtalk_bridge.orchestrator"):
        await orchestrator.handle_message("/stop", AUTHORIZED)
    blocked.set()
    await _wait_idle(orchestrator)
    lines = [r.getMessage() for r in caplog.records]
    assert any(
        "turn interrupted" in l and "reason=user_stop" in l for l in lines
    )


async def test_cmd_stop_does_not_log_interrupt_when_idle(caplog):
    # /stop on an idle orchestrator must NOT fabricate a fake interrupt
    # marker — there was nothing to interrupt.
    import logging
    orchestrator, _runner, _sent = build()
    with caplog.at_level(logging.INFO, logger="claude_dingtalk_bridge.orchestrator"):
        await orchestrator.handle_message("/stop", AUTHORIZED)
    lines = [r.getMessage() for r in caplog.records]
    assert not any("turn interrupted" in l for l in lines)


# --- TaskEvent unknown-phase fallback ---------------------------------------


async def test_emit_task_ignores_unknown_phase():
    # If the runner ever emits a TaskEvent with a phase we don't know
    # (forward-compat with future SDK additions) the orchestrator must
    # not crash. The phone gets nothing, the pending sets are untouched.
    from claude_dingtalk_bridge.claude_runner import TaskEvent
    orchestrator, _runner, sent = build()
    await orchestrator._emit_task(
        TaskEvent("future_phase", "t1", description="x")
    )
    assert sent == []
    assert orchestrator._pending_tasks == set()
    assert orchestrator._acknowledged_tasks == set()


async def test_emit_ignores_unknown_event_type():
    # Forward-compat: if ClaudeRunner ever emits a new event type the
    # orchestrator doesn't recognise, _emit falls through silently rather
    # than crashing the turn.
    orchestrator, _runner, sent = build()

    class _FutureEvent:
        pass

    await orchestrator._emit(_FutureEvent())
    assert sent == []


async def test_cmd_prompt_queues_when_queued_prompt_takes_over_mid_cancel():
    # Drain-cancel race: a fresh prompt arrives while the runner is draining
    # background-agent waits. cancel_drain() + await frees the current task,
    # but _run's finally already popped a queued prompt off the queue and
    # started a new task before the await returned. We must NOT skip queueing
    # this prompt — otherwise it would silently overlap the new task.
    orchestrator, _runner, sent = build()

    # Stage 1: an already-queued prompt that the runner will pop on finish.
    drain_release = asyncio.Event()
    second_started = asyncio.Event()

    async def slow_first_turn(project_path, prompt, emit):
        # Pretend to be in drain mode until cancel_drain is called.
        await drain_release.wait()

    async def slow_second_turn(project_path, prompt, emit):
        # Block forever so the second task is "running" when _cmd_prompt
        # checks self._task on the post-await line.
        second_started.set()
        await asyncio.Event().wait()

    call_log = []

    async def run_turn(project_path, prompt, emit):
        call_log.append(prompt)
        if len(call_log) == 1:
            await slow_first_turn(project_path, prompt, emit)
        else:
            await slow_second_turn(project_path, prompt, emit)

    _runner.run_turn = run_turn
    # Pretend we entered drain mode for the first task.
    _runner.is_draining = True

    def cancel_drain():
        # Releasing the await lets first_turn return, the finally block
        # pops the queued prompt, and the second turn starts.
        _runner.is_draining = False
        drain_release.set()
    _runner.cancel_drain = cancel_drain

    # Kick off the first task and immediately enqueue a second prompt so
    # _drain_queue has something to pop.
    await orchestrator.handle_message("first", AUTHORIZED)
    orchestrator._queue.append("queued-before-cancel")

    # Now a fresh prompt arrives mid-drain — _cmd_prompt should:
    #  1. Call cancel_drain → releases first turn
    #  2. await self._task → finally drains queue, starts the queued prompt
    #  3. See self._task is the NEW task (not None, not done)
    #  4. Append THIS new prompt to the queue and send a queued notice
    await orchestrator.handle_message("third", AUTHORIZED)
    # The second task must have started.
    await second_started.wait()
    # And this new prompt was appended after the takeover.
    assert "third" in orchestrator._queue
    assert any("Task running — queued" in m for m in sent)

    # Tidy up the still-running second task so the test fixture doesn't warn.
    orchestrator._task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await orchestrator._task


# --- _delayed_pending_notice race arms --------------------------------------


async def test_delayed_pending_notice_no_op_after_cancellation():
    # The notice timer is cancellable: if cancel lands during the sleep, the
    # coroutine exits cleanly without sending. Direct call so we don't have
    # to race the real timer.
    orchestrator, _runner, sent = build()
    task = asyncio.create_task(orchestrator._delayed_pending_notice())
    await asyncio.sleep(0)  # let it enter the sleep
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert sent == []


async def test_delayed_pending_notice_recheck_finds_nothing_after_sleep(monkeypatch):
    # Recheck path: the timer fires, but by then every pending task has
    # already been acknowledged. The notice is suppressed — otherwise we'd
    # warn about agents the SDK already considers done.
    monkeypatch.setattr(orch_mod, "_PENDING_NOTICE_DELAY", 0.01)
    orchestrator, _runner, sent = build()
    orchestrator._pending_tasks.add("t1")
    orchestrator._acknowledged_tasks.add("t1")
    await orchestrator._delayed_pending_notice()
    assert not any("background agent" in m for m in sent)
