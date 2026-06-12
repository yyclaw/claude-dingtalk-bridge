import asyncio
import contextlib
from pathlib import Path

import pytest

import claude_dingtalk_bridge.orchestrator as orch_mod
from claude_dingtalk_bridge.claude_runner import ResultEvent, TextEvent, ToolEvent
from claude_dingtalk_bridge.config import Config, GeoConfig, Project, load_config
from claude_dingtalk_bridge.orchestrator import Orchestrator
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
        permission_ask_timeout=600,
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
        self.session_costs: dict[str, float] = {}
        self.last_turn_costs: dict[str, float | None] = {}
        self.model_override = None
        self.observed_model = None
        self.permission_mode = None
        self.is_draining = False
        self.drain_cancels = 0
        self.turn_counts: dict[str, int] = {}

    def cancel_drain(self) -> None:
        self.drain_cancels += 1
        self.is_draining = False

    def set_model(self, model):
        self.model_override = model

    def set_permission_mode(self, mode):
        self.permission_mode = mode

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

    def session_cost(self, project_path: str) -> float:
        return self.session_costs.get(project_path, 0.0)

    def last_turn_cost(self, project_path: str) -> float | None:
        return self.last_turn_costs.get(project_path)

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


def build(runner=None, geo_check=None, geo_timeout=3):
    config = make_config()
    # In production geo_check is wired only when config.geo is set; mirror that
    # so the orchestrator can read geo.timeout_seconds for the slow-notice delay.
    if geo_check is not None:
        config.geo = GeoConfig(timeout_seconds=geo_timeout)
    runner = runner or FakeRunner()
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    orchestrator = Orchestrator(
        config=config,
        registry=ProjectRegistry(config.projects),
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


def _config_yaml(projects: list[tuple[str, str]]) -> str:
    lines = [
        "dingtalk:",
        "  client_id: k",
        "  client_secret: s",
        f"authorized_user_id: {AUTHORIZED}",
        "projects:",
    ]
    for name, path in projects:
        lines.append(f"  - name: {name}")
        lines.append(f"    path: {path}")
    return "\n".join(lines) + "\n"


def build_with_file(tmp_path, projects, runner=None):
    """Build an orchestrator backed by a real config file so /ls reload can
    re-read it. Returns (orchestrator, runner, sent, path)."""
    path = tmp_path / "config.yaml"
    path.write_text(_config_yaml(projects))
    path.chmod(0o600)
    config = load_config(path)
    runner = runner or FakeRunner()
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    orchestrator = Orchestrator(
        config=config,
        registry=ProjectRegistry(config.projects),
        runner=runner,
        send=send,
        send_markdown=send,
        config_path=path,
    )
    runner.permission_handler = orchestrator.request_permission
    runner.question_handler = orchestrator.answer_question
    return orchestrator, runner, sent, path


async def test_ls_reload_picks_up_new_project(tmp_path):
    orchestrator, runner, sent, path = build_with_file(
        tmp_path, [("multica", "/tmp/multica"), ("docs", "/tmp/docs")]
    )
    path.write_text(
        _config_yaml(
            [("multica", "/tmp/multica"), ("docs", "/tmp/docs"), ("api", "/tmp/api")]
        )
    )
    await orchestrator.handle_message("/ls reload", AUTHORIZED)
    assert "api" in orchestrator._registry.names()
    assert any("api" in m and "/tmp/api" in m for m in sent)


async def test_ls_reload_keeps_current_when_still_present(tmp_path):
    orchestrator, runner, sent, path = build_with_file(
        tmp_path, [("multica", "/tmp/multica"), ("docs", "/tmp/docs")]
    )
    assert orchestrator._current_project.name == "multica"
    path.write_text(
        _config_yaml([("multica", "/tmp/multica"), ("api", "/tmp/api")])
    )
    await orchestrator.handle_message("/ls reload", AUTHORIZED)
    assert orchestrator._current_project.name == "multica"
    # Current project preserved → no session reset.
    assert runner.resets == []


async def test_ls_reload_follows_current_path_change(tmp_path):
    orchestrator, runner, sent, path = build_with_file(
        tmp_path, [("multica", "/tmp/multica"), ("docs", "/tmp/docs")]
    )
    path.write_text(
        _config_yaml([("multica", "/tmp/multica-moved"), ("docs", "/tmp/docs")])
    )
    await orchestrator.handle_message("/ls reload", AUTHORIZED)
    assert orchestrator._current_project.name == "multica"
    assert orchestrator._current_project.path == "/tmp/multica-moved"


async def test_ls_reload_falls_back_to_default_when_current_removed(tmp_path):
    orchestrator, runner, sent, path = build_with_file(
        tmp_path, [("multica", "/tmp/multica"), ("docs", "/tmp/docs")]
    )
    orchestrator._current_project = orchestrator._registry.get("docs")
    path.write_text(
        _config_yaml([("multica", "/tmp/multica"), ("api", "/tmp/api")])
    )
    await orchestrator.handle_message("/ls reload", AUTHORIZED)
    assert orchestrator._current_project.name == "multica"
    # Fallback runs /cd, which resets the now-current project's session and
    # announces the switch.
    assert "/tmp/multica" in runner.resets
    assert any("Switched to" in m and "multica" in m for m in sent)


async def test_ls_reload_reports_error_and_keeps_registry(tmp_path):
    orchestrator, runner, sent, path = build_with_file(
        tmp_path, [("multica", "/tmp/multica"), ("docs", "/tmp/docs")]
    )
    path.write_text("projects: []\n")
    await orchestrator.handle_message("/ls reload", AUTHORIZED)
    assert any("Reload failed" in m for m in sent)
    assert orchestrator._registry.names() == ["multica", "docs"]


async def test_ls_reload_rejects_duplicate_names_and_keeps_registry(tmp_path):
    # A hand-edited config with duplicate names must fail the reload cleanly —
    # the user is editing from their phone, so they need a message, not a crash,
    # and the live projects must survive untouched.
    orchestrator, runner, sent, path = build_with_file(
        tmp_path, [("multica", "/tmp/multica"), ("docs", "/tmp/docs")]
    )
    path.write_text(
        _config_yaml([("multica", "/tmp/multica"), ("multica", "/tmp/other")])
    )
    await orchestrator.handle_message("/ls reload", AUTHORIZED)
    assert any("Reload failed" in m and "multica" in m for m in sent)
    assert orchestrator._registry.names() == ["multica", "docs"]


async def test_ls_unknown_arg_shows_usage(tmp_path):
    orchestrator, runner, sent, path = build_with_file(
        tmp_path, [("multica", "/tmp/multica")]
    )
    await orchestrator.handle_message("/ls bogus", AUTHORIZED)
    assert any("Usage" in m and "reload" in m for m in sent)


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
    assert any("(empty)" in m for m in sent)
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


async def test_permission_escalates_and_resolves_on_approve():
    orchestrator, runner, sent = build()

    async def approve_soon():
        await asyncio.sleep(0)
        await orchestrator.handle_message("ok", AUTHORIZED)

    result, _ = await asyncio.gather(
        orchestrator.request_permission("Bash", {"command": "rm x"}),
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
        orchestrator.request_permission("Bash", {"command": "rm x"}),
        deny_soon(),
    )
    assert result is False


async def test_permission_timeout_denies():
    orchestrator, runner, sent = build()
    orchestrator._config.permission_ask_timeout = 0
    result = await orchestrator.request_permission("Bash", {"command": "rm x"})
    assert result is False
    assert any("timed out" in m for m in sent)


async def test_permission_prompt_wraps_command_in_code_fence():
    """A heredoc with a leading `# …` comment line must not render as an H1.

    Regression: the prompt used to interpolate the raw command directly into
    the markdown body, so a Python/shell comment line at column 0 was parsed
    by DingTalk's markdown renderer as a level-1 heading and shown at giant
    font size.
    """
    orchestrator, _, _, md_sent = build_channels()
    command = (
        "cat > /tmp/x.py << 'EOF'\n"
        "import re\n"
        "\n"
        "# Read orchestrator.py and extract _send calls\n"
        "EOF"
    )

    async def approve_soon():
        while orchestrator._permission_future is None or orchestrator._permission_future.done():
            await asyncio.sleep(0)
        await orchestrator.handle_message("ok", AUTHORIZED)

    await asyncio.gather(
        orchestrator.request_permission("Bash", {"command": command}),
        approve_soon(),
    )
    prompt = next(m for m in md_sent if "Permission needed" in m)
    assert f"```\n{command}\n```" in prompt
    # No bare `# ` line outside the fence (would render as H1).
    fence_open = prompt.index("```")
    fence_close = prompt.index("```", fence_open + 3)
    outside = prompt[:fence_open] + prompt[fence_close + 3:]
    assert not any(
        line.lstrip().startswith("# ") and not line.lstrip().startswith("###")
        for line in outside.splitlines()
    )


async def test_permission_prompt_keeps_lock_for_ordinary_command():
    orchestrator, _, _, md_sent = build_channels()

    async def approve_soon():
        while orchestrator._permission_future is None or orchestrator._permission_future.done():
            await asyncio.sleep(0)
        await orchestrator.handle_message("ok", AUTHORIZED)

    await asyncio.gather(
        orchestrator.request_permission("Bash", {"command": "git push"}),
        approve_soon(),
    )
    prompt = next(m for m in md_sent if "Permission needed" in m)
    assert "🔐" in prompt
    assert "‼️" not in prompt


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
        orchestrator.request_permission("Bash", {"command": "rm a"}),
        orchestrator.request_permission("Bash", {"command": "rm b"}),
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


async def test_geo_skip_notice_send_blip_does_not_relabel_as_aborted():
    # When the geo check fails AND the "Turn skipped" notice send itself blips,
    # the send failure must be swallowed in place — not fall through to the
    # outer handler, which would relabel a clean geo skip as "Turn aborted"
    # carrying the transport error and burying check.detail's network guidance.
    async def geo_check():
        from claude_dingtalk_bridge.geo import GeoCheck
        return GeoCheck(ok=False, detail="📍 IP: 45.8.1.1\n❌ IP location: HK (expected: US)")

    orchestrator, runner, sent = build(geo_check=geo_check)

    real_send = orchestrator._send

    async def blip_on_skip_notice(text: str) -> None:
        if "skipped" in text:
            raise ConnectionError("proxy blip")
        await real_send(text)

    orchestrator._send = blip_on_skip_notice
    await orchestrator.handle_message("do work", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert runner.turns == []
    # The blip stayed contained: no misleading "Turn aborted" leaked through.
    assert not any("Turn aborted" in m for m in sent)


async def test_geo_failure_with_live_timer_cancels_it(monkeypatch):
    """Failure path while the slow-notice timer is live (delay not None): the
    timer is cancelled in finally before the 'Turn skipped' notice, and no
    stray 'still checking' leaks. Failure tests above use a short timeout where
    no timer is ever created — this exercises the timer-present branch."""
    monkeypatch.setattr(orch_mod, "_geo_slow_notice_delay", lambda t: 5.0)

    async def geo_check():
        from claude_dingtalk_bridge.geo import GeoCheck
        await asyncio.sleep(0)  # let the timer enter its sleep first
        return GeoCheck(ok=False, detail="📍 IP: 45.8.1.1\n❌ IP location: HK (expected: US)")

    orchestrator, runner, sent = build(geo_check=geo_check, geo_timeout=10)
    await orchestrator.handle_message("do work", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert runner.turns == []
    assert any("skipped" in m and "HK" in m for m in sent)
    assert not any("Checking geo location" in m for m in sent)


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


def test_geo_slow_notice_delay_rule():
    """Below the floor → no notice; above it → 60% of the timeout, rounded,
    always leaving headroom before the request's own timeout."""
    assert orch_mod._geo_slow_notice_delay(3) is None    # below floor
    assert orch_mod._geo_slow_notice_delay(5) is None    # at the floor (==)
    assert orch_mod._geo_slow_notice_delay(6) == 4.0     # just above floor (3.6↑)
    assert orch_mod._geo_slow_notice_delay(7) == 4.0     # rounds down (4.2↓)
    assert orch_mod._geo_slow_notice_delay(8) == 5.0     # rounds up (4.8↑)
    assert orch_mod._geo_slow_notice_delay(10) == 6.0    # exact (6.0)
    # All headroom: the delay is strictly less than the timeout for every value
    # above the floor, so the notice can never coincide with the request's own
    # timeout (the degeneracy the old fixed 5.0 == default timeout suffered).
    for t in range(6, 60):
        assert orch_mod._geo_slow_notice_delay(t) < t


async def test_geo_short_timeout_skips_slow_notice():
    """A timeout at/below the floor gets no slow notice even when the check is
    slow — the degenerate case where a fixed delay == timeout used to misfire."""
    async def geo_check():
        from claude_dingtalk_bridge.geo import GeoCheck
        await asyncio.sleep(0.02)
        return GeoCheck(ok=True, detail="✅ IP location verified: US")

    orchestrator, runner, sent = build(geo_check=geo_check, geo_timeout=5)
    await orchestrator.handle_message("do work", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert not any("Checking geo location" in m for m in sent)
    assert runner.turns == [("/tmp/multica", "do work")]


async def test_geo_slow_check_pushes_wait_notice(monkeypatch):
    monkeypatch.setattr(orch_mod, "_geo_slow_notice_delay", lambda t: 0.01)

    async def geo_check():
        from claude_dingtalk_bridge.geo import GeoCheck
        await asyncio.sleep(0.05)
        return GeoCheck(ok=True, detail="✅ IP location verified: US")

    orchestrator, runner, sent = build(geo_check=geo_check, geo_timeout=10)
    await orchestrator.handle_message("do work", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert any("Checking geo location" in m for m in sent)
    assert runner.turns == [("/tmp/multica", "do work")]


async def test_geo_fast_check_stays_silent(monkeypatch):
    monkeypatch.setattr(orch_mod, "_geo_slow_notice_delay", lambda t: 0.05)

    async def geo_check():
        from claude_dingtalk_bridge.geo import GeoCheck
        return GeoCheck(ok=True, detail="✅ IP location verified: US")

    orchestrator, runner, sent = build(geo_check=geo_check, geo_timeout=10)
    await orchestrator.handle_message("do work", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert not any("Checking geo location" in m for m in sent)


async def test_geo_notice_timer_cancelled_after_entering_sleep(monkeypatch):
    """The timer's CancelledError arm: geo settles after the timer has already
    entered its sleep, so finally's cancel() unwinds it through the except (vs
    test_geo_fast_check_stays_silent, where the check returns without ever
    yielding so the timer is cancelled before it starts)."""
    monkeypatch.setattr(orch_mod, "_geo_slow_notice_delay", lambda t: 5.0)

    async def geo_check():
        from claude_dingtalk_bridge.geo import GeoCheck
        # Yield once so the slow-notice task gets to start its sleep before the
        # check returns and finally cancels it mid-sleep.
        await asyncio.sleep(0)
        return GeoCheck(ok=True, detail="✅ IP location verified: US")

    orchestrator, runner, sent = build(geo_check=geo_check, geo_timeout=10)
    await orchestrator.handle_message("do work", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert not any("Checking geo location" in m for m in sent)
    assert runner.turns == [("/tmp/multica", "do work")]


async def test_geo_interrupt_clears_slow_notice(monkeypatch):
    """/clear (or /stop) mid-geo cancels the turn, which clears the timer."""
    monkeypatch.setattr(orch_mod, "_geo_slow_notice_delay", lambda t: 1.0)

    async def geo_check():
        from claude_dingtalk_bridge.geo import GeoCheck
        await asyncio.sleep(5.0)
        return GeoCheck(ok=True, detail="✅ IP location verified: US")

    orchestrator, runner, sent = build(geo_check=geo_check, geo_timeout=10)
    await orchestrator.handle_message("do work", AUTHORIZED)
    task = orchestrator._task
    await orchestrator.handle_message("/clear", AUTHORIZED)
    with contextlib.suppress(asyncio.CancelledError):
        await task
    # let the cancelled slow-notice timer settle before asserting
    await asyncio.sleep(0)
    assert not any("Checking geo location" in m for m in sent)
    assert runner.turns == []


async def test_geo_slow_notice_suppressed_when_turn_cancelled():
    """The notice checks _turn_cancelled before sending, so a /stop racing the
    timer (it fires during interrupt()'s await) doesn't push reassurance just
    before the 'Task stopped' ack."""
    orchestrator, runner, sent = build()
    orchestrator._turn_cancelled = True
    await orchestrator._geo_slow_notice(0.0)
    assert not any("Checking geo location" in m for m in sent)


async def test_geo_slow_notice_sends_when_active():
    orchestrator, runner, sent = build()
    orchestrator._turn_cancelled = False
    await orchestrator._geo_slow_notice(0.0)
    assert any("Checking geo location" in m for m in sent)


async def test_geo_stop_replies_and_clears_task():
    """/stop mid-geo must still ack the phone and reset self._task to None.

    Regression: _drain_queue used to live only in the run_turn finally, so a
    cancel landing during the geo await left self._task non-None and _cmd_stop
    sent nothing.
    """
    async def geo_check():
        from claude_dingtalk_bridge.geo import GeoCheck
        await asyncio.sleep(5.0)
        return GeoCheck(ok=True, detail="✅ IP location verified: US")

    orchestrator, runner, sent = build(geo_check=geo_check)
    await orchestrator.handle_message("do work", AUTHORIZED)
    # Let the turn task actually reach the geo await before /stop fires —
    # in production the two messages arrive on separate WebSocket events with
    # event-loop ticks between them, but in a single test coroutine we need
    # to yield once explicitly so cancel hits a running coroutine (whose
    # finally can run) rather than one that never started.
    await asyncio.sleep(0)
    await orchestrator.handle_message("/stop", AUTHORIZED)
    assert orchestrator._task is None
    # Phone gets a "Task stopped" ack — but NOT the "say go on to continue"
    # hint, because nothing was ever started (geo phase aborted).
    assert any("Task stopped" in m for m in sent)
    assert not any("go on" in m for m in sent)
    assert runner.turns == []


async def test_geo_stop_auto_advances_queue():
    """A bare /stop mid-geo lets the queued prompt auto-start, matching the
    documented behavior of /stop (interrupt + auto-advance)."""
    geo_calls = 0

    async def geo_check():
        from claude_dingtalk_bridge.geo import GeoCheck
        nonlocal geo_calls
        geo_calls += 1
        if geo_calls == 1:
            await asyncio.sleep(5.0)
        return GeoCheck(ok=True, detail="✅ IP location verified: US")

    orchestrator, runner, sent = build(geo_check=geo_check)
    await orchestrator.handle_message("first", AUTHORIZED)
    await asyncio.sleep(0)  # let the first turn reach the geo await
    # Queue a second prompt while the first is still in geo.
    orchestrator._queue.append("second")
    await orchestrator.handle_message("/stop", AUTHORIZED)
    # Let the auto-advanced second turn complete its (now-fast) geo + run_turn.
    if orchestrator._task is not None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await orchestrator._task
    assert runner.turns == [("/tmp/multica", "second")]


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
    orchestrator._config.permission_ask_timeout = 0
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
    assert any('`/resume` first' in m for m in sent)


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
    assert "**Cache last turn:** cached 45K (84.9%) · new 8K" in msg


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
    assert "**Cache last turn:** cached 45K (84.7%) · new 8K" in msg
    assert "  - opus-4.7[1m]: cached 45K (86.4%) · new 7K" in msg
    assert "  - haiku-4.5: cached 0 (0.0%) · new 1K" in msg


async def test_status_cost_first_when_cost_available():
    # Option B: when ResultMessage carries total_cost_usd, the parent line
    # leads with dollars and demotes tokens to a parenthetical, and the cache
    # line prefixes the turn's cost. Per-model sub-bullets keep showing
    # tokens — SDK doesn't expose per-model cost.
    runner = FakeRunner()
    runner.token_totals["/tmp/multica"] = 7_700_000
    runner.session_costs["/tmp/multica"] = 22.4
    runner.last_turn_costs["/tmp/multica"] = 7.32
    runner.usages["/tmp/multica"] = {
        "cache_read_input_tokens": 2_100_000,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 109_000,
            "ephemeral_5m_input_tokens": 0,
        },
    }
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("/status", AUTHORIZED)
    msg = "\n".join(sent)
    assert "**Session cost:** ~$22.40 (7.7M tokens)" in msg
    assert "**Cache last turn:** ~$7.32 · cached 2.1M" in msg
    assert "Session tokens" not in msg


async def test_status_cost_first_keeps_per_model_token_breakdown():
    runner = FakeRunner()
    runner.token_totals["/tmp/multica"] = 1_700_000
    runner.session_costs["/tmp/multica"] = 5.0
    runner.last_turn_costs["/tmp/multica"] = 1.25
    runner.model_token_totals["/tmp/multica"] = {
        "claude-opus-4-7": 1_100_000,
        "claude-opus-4-7[1m]": 572_200,
    }
    runner.usages["/tmp/multica"] = {
        "cache_read_input_tokens": 1_000_000,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 64_100,
            "ephemeral_5m_input_tokens": 0,
        },
    }
    runner.model_usages["/tmp/multica"] = {
        "claude-opus-4-7": {
            "inputTokens": 10, "outputTokens": 200,
            "cacheReadInputTokens": 700_000, "cacheCreationInputTokens": 40_000,
        },
        "claude-opus-4-7[1m]": {
            "inputTokens": 5, "outputTokens": 100,
            "cacheReadInputTokens": 300_000, "cacheCreationInputTokens": 24_100,
        },
    }
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("/status", AUTHORIZED)
    msg = "\n".join(sent)
    assert "**Session cost:** ~$5.00 (1.7M tokens)" in msg
    # Per-model sub-bullets keep showing tokens, NOT a fake $-amount.
    assert "  - opus-4.7: 1.1M" in msg
    assert "  - opus-4.7[1m]: 572.2K" in msg
    assert "  - opus-4.7: cached 700K" in msg
    assert "  - opus-4.7[1m]: cached 300K" in msg


async def test_status_session_cost_with_unknown_last_turn_cost():
    # Earlier turns had cost but the latest didn't (SDK omitted it). Parent
    # line still uses the session total; cache line drops the $ prefix so it
    # doesn't lie about the unknown.
    runner = FakeRunner()
    runner.token_totals["/tmp/multica"] = 100_000
    runner.session_costs["/tmp/multica"] = 0.42
    runner.last_turn_costs["/tmp/multica"] = None
    runner.usages["/tmp/multica"] = {
        "cache_read_input_tokens": 50_000,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 5000,
            "ephemeral_5m_input_tokens": 0,
        },
    }
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("/status", AUTHORIZED)
    msg = "\n".join(sent)
    assert "**Session cost:** ~$0.42" in msg
    assert "**Cache last turn:** cached 50K" in msg
    assert "$ · cached" not in msg


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


async def test_model_switch_by_name():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/model opus", AUTHORIZED)
    assert runner.model_override == "opus"


async def test_model_list_marks_current():
    orchestrator, runner, sent = build()
    runner.model_override = "sonnet"
    await orchestrator.handle_message("/model", AUTHORIZED)
    assert any("(current)" in m for m in sent)


async def test_mode_no_arg_lists_all_modes():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/mode", AUTHORIZED)
    msg = "\n".join(sent)
    for name in ("default", "acceptEdits", "bypassPermissions", "plan", "reset"):
        assert name in msg, f"{name} missing from /mode listing"


async def test_mode_no_arg_marks_current_when_set():
    orchestrator, runner, sent = build()
    runner.permission_mode = "acceptEdits"
    await orchestrator.handle_message("/mode", AUTHORIZED)
    msg = "\n".join(sent)
    # The current row carries the marker; the others don't.
    assert "acceptEdits` (current)" in msg
    assert "plan` (current)" not in msg


async def test_mode_no_arg_omits_current_on_reset_when_unset():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/mode", AUTHORIZED)
    msg = "\n".join(sent)
    assert "reset" in msg
    assert "(current)" not in msg


@pytest.mark.parametrize(
    "mode", ["default", "acceptEdits", "bypassPermissions", "plan"]
)
async def test_mode_set_valid(mode):
    orchestrator, runner, sent = build()
    await orchestrator.handle_message(f"/mode {mode}", AUTHORIZED)
    assert runner.permission_mode == mode
    assert any(mode in m for m in sent)


async def test_mode_reset_clears_override():
    orchestrator, runner, sent = build()
    runner.permission_mode = "plan"
    await orchestrator.handle_message("/mode reset", AUTHORIZED)
    assert runner.permission_mode is None
    assert any("cleared" in m for m in sent)


async def test_mode_unknown_value_rejected():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/mode wild", AUTHORIZED)
    assert runner.permission_mode is None
    assert any("Unknown mode" in m for m in sent)


async def test_status_shows_model():
    orchestrator, runner, sent = build()
    runner.model_override = "haiku"
    await orchestrator.handle_message("/status", AUTHORIZED)
    assert any("**Model:** haiku" in m for m in sent)


async def test_status_shows_default_model_when_unset():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/status", AUTHORIZED)
    assert any("**Model:** SDK default" in m for m in sent)


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


async def test_forward_intent_narration_dropped_in_brief_mode():
    # The real-world leak: period-terminated "going to do X" lines slipped past
    # the old colon-only check and flooded the phone. Every sentence here opens
    # with a forward-intent marker, so brief mode drops them.
    runner = FakeRunner()
    runner.script = [
        TextEvent("Now the ThemeProvider."),
        TextEvent("Let me read the key files in parallel."),
        TextEvent("I'll wrap App renders in ThemeProvider via a helper."),
        TextEvent("Now let me run the frontend tests and typecheck."),
    ]
    orchestrator, runner, sent = build(runner)
    assert orchestrator._verbose is False
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert not any("ThemeProvider" in m for m in sent)
    assert not any("key files" in m for m in sent)
    assert not any("wrap App renders" in m for m in sent)
    assert not any("frontend tests" in m for m in sent)


async def test_observation_with_forward_tail_dropped():
    # A leading observation followed by "Let me X" / "Now I'll Y" is mostly
    # intent dressing — drop the whole block. The 📋 Tasks checklist and
    # subagent notices are the progress signal in brief mode, not prose.
    runner = FakeRunner()
    runner.script = [
        # Image leak (bubble 1): observation then two forward intents.
        TextEvent(
            "I can see DingTalk messages where the daemon is pushing Japanese "
            "intermediate narration to the phone. Let me investigate where "
            "Japanese could come from. Let me look at the prompt construction path."
        ),
        # Image leak (bubble 2): observation then "Let me find this".
        TextEvent(
            "The image shows a previous session where the daemon was pushing "
            "Japanese narrations. Let me find this in the logs."
        ),
        # Milestone + forward tail also dropped under the stricter rule.
        TextEvent("Task 1 wired up. Now the Tooltip component."),
        TextEvent("Build succeeds. Let me verify the compiled CSS."),
    ]
    orchestrator, runner, sent = build(runner)
    assert orchestrator._verbose is False
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert not any("Japanese" in m for m in sent)
    assert not any("image shows" in m for m in sent)
    assert not any("Task 1 wired up" in m for m in sent)
    assert not any("Build succeeds" in m for m in sent)


async def test_pure_result_block_without_forward_tail_kept():
    # Symmetric guard: a block that reports an outcome with no "Let me X" /
    # "Now I'll Y" tail must still reach the phone — it's the final reply.
    runner = FakeRunner()
    runner.script = [
        TextEvent(
            "All four changes are implemented, tested, and verified live in "
            "the browser."
        ),
    ]
    orchestrator, runner, sent = build(runner)
    assert orchestrator._verbose is False
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert any("verified live" in m for m in sent)


async def test_forward_intent_narration_kept_in_verbose_mode():
    # Verbose bypasses the filler filter — full progress is the point.
    runner = FakeRunner()
    runner.script = [TextEvent("Now the ThemeProvider.")]
    orchestrator, runner, sent = build(runner)
    orchestrator._verbose = True
    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert any("ThemeProvider" in m for m in sent)


def test_is_progress_filler_ignores_dots_inside_filenames():
    # The dot in `main.tsx` / `index.html` must not split the sentence — else
    # a non-forward fragment ("tsx and add …") wrongly keeps a forward block.
    assert orch_mod._is_progress_filler(
        "Now wire ThemeProvider into main.tsx and add a script in index.html."
    ) is True
    assert orch_mod._is_progress_filler(
        "Now use Tooltip in Card.tsx, replacing the native title."
    ) is True


def test_is_progress_filler_keeps_punctuation_only_block():
    # A block that splits into no real sentences (pure punctuation) reports
    # nothing to match against — default to keeping it rather than dropping.
    assert orch_mod._is_progress_filler("...") is False
    assert orch_mod._is_progress_filler("Let me check.") is True
    assert orch_mod._is_progress_filler("Done.") is False


def test_is_progress_filler_keeps_genuine_sequenced_prose():
    # Pure sequencers open ordinary answer prose, not tool intent — a complete,
    # self-contained reply must survive even when a sentence starts with one.
    assert orch_mod._is_progress_filler(
        "Here are the steps. First, generate a key with ssh-keygen. "
        "Then copy it to the server. Finally test the connection."
    ) is False
    assert orch_mod._is_progress_filler(
        "Deleting that directory is safe. Also, a copy exists in git history."
    ) is False
    assert orch_mod._is_progress_filler(
        "Renamed the functions and the tests pass. Next, run make lint."
    ) is False


def test_is_progress_filler_matches_typographic_apostrophe():
    # The model often emits a curly apostrophe (U+2019); "I’ll"/"let’s" must
    # drop the same as their ASCII forms, else the narration leaks to the phone.
    assert orch_mod._is_progress_filler("I’ll check the config.") is True
    assert orch_mod._is_progress_filler("Let’s read the file.") is True
    assert orch_mod._is_progress_filler("I’m going to run the tests.") is True


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


async def test_failed_task_notice_send_blip_does_not_relabel_as_aborted():
    # When run_turn fails AND the "Task failed" notice send itself blips, the
    # send failure must be swallowed in place — not fall through to the outer
    # handler, which would relabel a genuine runner failure as "Turn aborted"
    # carrying the transport error instead of the real one.
    runner = FakeRunner()

    async def failing_run_turn(project_path, prompt, emit):
        raise RuntimeError("turn blew up")

    runner.run_turn = failing_run_turn
    orchestrator, runner, sent = build(runner)

    real_send = orchestrator._send

    async def send_blip_on_failure_notice(text: str) -> None:
        if "Task failed" in text:
            raise ConnectionError("proxy blip")
        await real_send(text)

    orchestrator._send = send_blip_on_failure_notice

    await orchestrator.handle_message("go", AUTHORIZED)
    await _wait_idle(orchestrator)
    # The blip stayed contained: no misleading "Turn aborted" leaked through.
    assert not any("Turn aborted" in m for m in sent)


async def test_run_swallows_send_failure_before_turn():
    # A transient transport blip on the task-started banner (the access-token
    # POST failing through the proxy) used to escape the fire-and-forget _run
    # task — asyncio logged an unretrieved-task exception and the turn vanished
    # with no phone feedback. _run must never let one bad send kill the task,
    # even when the error notice itself can't be delivered.
    orchestrator, runner, sent = build()

    async def always_failing_send(text: str) -> None:
        raise ConnectionError("proxy blip")

    orchestrator._send = always_failing_send
    orchestrator._send_markdown = always_failing_send

    await orchestrator.handle_message("go", AUTHORIZED)
    task = orchestrator._task
    await asyncio.gather(task, return_exceptions=True)
    assert task.exception() is None


async def test_geo_slow_notice_swallows_send_failure():
    # The slow-notice runs as a fire-and-forget timer task whose exception is
    # never retrieved — a transient transport blip on its reassurance _send
    # must not escape it.
    orchestrator, runner, sent = build()

    async def failing_send(text: str) -> None:
        raise ConnectionError("proxy blip")

    orchestrator._send = failing_send
    orchestrator._turn_cancelled = False
    await orchestrator._geo_slow_notice(0)


async def test_delayed_pending_notice_swallows_send_failure(monkeypatch):
    # Same fire-and-forget timer hazard for the "background agent still
    # running" reassurance notice.
    monkeypatch.setattr(orch_mod, "_PENDING_NOTICE_DELAY", 0)
    orchestrator, runner, sent = build()
    orchestrator._pending_tasks = {"t1"}
    orchestrator._acknowledged_tasks = set()
    orchestrator._turn_cancelled = False

    async def failing_send(text: str) -> None:
        raise ConnectionError("proxy blip")

    orchestrator._send = failing_send
    await orchestrator._delayed_pending_notice()


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
    # Escalation and reply log at INFO so an operator can trace what was
    # asked and how long it took.
    import logging
    orchestrator, _runner, sent = build()
    with caplog.at_level(logging.INFO, logger="claude_dingtalk_bridge.orchestrator"):
        task = asyncio.create_task(
            orchestrator.request_permission("Bash", {"command": "rm -rf /tmp/x"})
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
            orchestrator.request_permission("Bash", {"command": "rm -rf /tmp/x"})
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
    monkeypatch.setattr(orchestrator._config, "permission_ask_timeout", 0.05)
    with caplog.at_level(logging.INFO, logger="claude_dingtalk_bridge.orchestrator"):
        result = await orchestrator.request_permission(
            "Bash", {"command": "rm -rf /tmp/x"}
        )
    assert result is False
    lines = [r.getMessage() for r in caplog.records]
    assert any("permission timeout" in l for l in lines)


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


async def test_notify_pushes_through_plain_text_channel():
    orchestrator, _runner, text_sent, md_sent = build_channels()
    await orchestrator.notify("📷 image failed")
    assert text_sent == ["📷 image failed"]
    assert md_sent == []


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


# --- /stop all & queue management -------------------------------------------


def _blocking_runner(block_prompt="first"):
    """A FakeRunner whose run_turn records turns (like the real one) but hangs
    on `block_prompt` until its gate is set — so a turn stays in-flight while
    the test queues more prompts and issues /stop."""
    gate = asyncio.Event()
    runner = FakeRunner()

    async def blocking_run_turn(project_path, prompt, emit):
        runner.turns.append((project_path, prompt))
        if prompt == block_prompt:
            await gate.wait()
        else:
            await emit(ResultEvent("", False))

    runner.run_turn = blocking_run_turn
    return runner, gate


async def _spin(n=5):
    for _ in range(n):
        await asyncio.sleep(0)


async def test_stop_all_clears_queue_without_resetting_session():
    runner, gate = _blocking_runner()
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("first", AUTHORIZED)
    await _spin()
    orchestrator._queue = ["queued one", "queued two"]
    await orchestrator.handle_message("/stop all", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert runner.interrupts == 1
    assert orchestrator._queue == []
    assert runner.resets == []  # session kept, unlike /clear
    assert any("queue" in m.lower() for m in sent)


async def test_stop_all_prevents_auto_advance_to_queued_prompt():
    # The whole point of /stop all: the queued prompt must NOT auto-start the
    # way it does after a bare /stop. Clearing the queue before the abort is
    # what stops _run's finally from popping it.
    runner, gate = _blocking_runner()
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("first", AUTHORIZED)
    await _spin()
    await orchestrator.handle_message("second", AUTHORIZED)
    assert orchestrator._queue == ["second"]
    await orchestrator.handle_message("/stop all", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert orchestrator._queue == []
    assert runner.turns == [("/tmp/multica", "first")]  # "second" never ran


async def test_stop_all_when_idle_clears_queue():
    # No turn running, but prompts are queued (e.g. left over). /stop all
    # should still drop them and say so.
    orchestrator, runner, sent = build()
    orchestrator._queue = ["one", "two"]
    await orchestrator.handle_message("/stop all", AUTHORIZED)
    assert orchestrator._queue == []
    assert any("Cleared" in m and "2" in m for m in sent)


async def test_bare_stop_still_auto_advances_queue():
    # Regression guard: /stop (no arg) keeps the existing behaviour — the next
    # queued prompt takes over.
    runner, gate = _blocking_runner()
    orchestrator, runner, sent = build(runner)
    await orchestrator.handle_message("first", AUTHORIZED)
    await _spin()
    await orchestrator.handle_message("second", AUTHORIZED)
    await orchestrator.handle_message("/stop", AUTHORIZED)
    await _wait_idle(orchestrator)
    assert ("/tmp/multica", "second") in runner.turns


async def test_queue_view_lists_numbered_prompts():
    orchestrator, runner, sent = build()
    orchestrator._queue = ["alpha task", "beta task"]
    await orchestrator.handle_message("/queue", AUTHORIZED)
    text = "\n".join(sent)
    assert "1" in text and "alpha task" in text
    assert "2" in text and "beta task" in text


async def test_queue_view_empty():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/queue", AUTHORIZED)
    assert any("empty" in m.lower() for m in sent)


async def test_queue_rm_removes_nth_prompt():
    orchestrator, runner, sent = build()
    orchestrator._queue = ["one", "two", "three"]
    await orchestrator.handle_message("/queue rm 2", AUTHORIZED)
    assert orchestrator._queue == ["one", "three"]
    assert any("two" in m for m in sent)


async def test_queue_rm_out_of_range_reports_error():
    orchestrator, runner, sent = build()
    orchestrator._queue = ["only"]
    await orchestrator.handle_message("/queue rm 5", AUTHORIZED)
    assert orchestrator._queue == ["only"]  # unchanged
    assert any("5" in m and "1" in m for m in sent)


async def test_queue_rm_non_number_reports_usage():
    orchestrator, runner, sent = build()
    orchestrator._queue = ["only"]
    await orchestrator.handle_message("/queue rm abc", AUTHORIZED)
    assert orchestrator._queue == ["only"]
    assert any("/queue" in m for m in sent)


async def test_queue_rm_all_clears():
    orchestrator, runner, sent = build()
    orchestrator._queue = ["one", "two"]
    await orchestrator.handle_message("/queue rm all", AUTHORIZED)
    assert orchestrator._queue == []
    assert any("2" in m for m in sent)


async def test_queue_rm_tolerates_extra_interior_whitespace():
    orchestrator, runner, sent = build()
    orchestrator._queue = ["one", "two", "three"]
    await orchestrator.handle_message("/queue rm   2", AUTHORIZED)
    assert orchestrator._queue == ["one", "three"]


async def test_queue_rm_all_tolerates_extra_interior_whitespace():
    orchestrator, runner, sent = build()
    orchestrator._queue = ["one", "two"]
    await orchestrator.handle_message("/queue  rm   all", AUTHORIZED)
    assert orchestrator._queue == []


async def test_queue_clear_clears():
    orchestrator, runner, sent = build()
    orchestrator._queue = ["one", "two", "three"]
    await orchestrator.handle_message("/queue clear", AUTHORIZED)
    assert orchestrator._queue == []


async def test_queue_clear_when_empty():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/queue clear", AUTHORIZED)
    assert any("empty" in m.lower() for m in sent)


async def test_queue_unknown_subcommand_shows_usage():
    orchestrator, runner, sent = build()
    orchestrator._queue = ["one"]
    await orchestrator.handle_message("/queue frobnicate", AUTHORIZED)
    assert orchestrator._queue == ["one"]
    assert any("/queue" in m for m in sent)


# --- /help <command> --------------------------------------------------------


async def test_help_lists_each_command_once():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/help", AUTHORIZED)
    # The trailing "/help <command>" footer is a usage hint, not a list entry;
    # check the command list above it.
    body = "\n".join(sent).split("💬")[0]
    for name in ("/stop", "/clear", "/queue", "/debug", "/model"):
        assert body.count(name) == 1, f"{name} should appear exactly once"


async def test_help_list_omits_self_referential_help_entry():
    # /help itself isn't a list row — the footer already explains it, so the
    # only "/help" in the output is that trailing hint.
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/help", AUTHORIZED)
    text = "\n".join(sent)
    assert text.count("/help") == 1
    assert "/help <command>" in text  # the footer hint survives


async def test_help_detail_shows_command_usage():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/help queue", AUTHORIZED)
    text = "\n".join(sent)
    assert "rm" in text and "clear" in text


async def test_help_detail_accepts_leading_slash():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/help /resume", AUTHORIZED)
    assert any("session" in m.lower() for m in sent)


async def test_help_unknown_command_points_to_help():
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/help nope", AUTHORIZED)
    assert any("/help" in m for m in sent)


async def test_help_detail_for_command_without_extra_detail():
    # /pwd has only a one-line brief, no detail block — brief is the fallback.
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/help pwd", AUTHORIZED)
    assert any("/pwd" in m and "working directory" in m for m in sent)


async def test_help_detail_prefers_detail_over_brief():
    # When a command has a detail block, the detail page shows it instead of
    # the one-line brief (brief is only the fallback). Syntax stays.
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("/help cd", AUTHORIZED)
    text = "\n".join(sent)
    assert "/cd <name>" in text          # syntax line
    assert "resets its session" in text  # detail
    assert "Switch working directory" not in text  # brief suppressed


# --- /update (self-update) ---------------------------------------------

import claude_dingtalk_bridge.self_update as su  # noqa: E402


def _compare(behind, subjects=()):
    return su.CompareResult(behind=behind, subjects=list(subjects))


def _patch_self_update(
    monkeypatch,
    *,
    compare=None,
    snapshots=None,
    pull_raises=None,
    make_raises=None,
):
    """Wire the self_update module the orchestrator calls into deterministic
    fakes, recording what ran. Returns a dict of recorded calls."""
    rec = {"pull": 0, "make": [], "restart": 0}

    async def fake_fetch(*_a, **_k):
        if isinstance(compare, Exception):
            raise compare
        return compare

    monkeypatch.setattr(su, "fetch_and_compare", fake_fetch)

    if snapshots is not None:
        it = iter(snapshots)
        monkeypatch.setattr(su, "snapshot", lambda *_a, **_k: next(it))

    async def fake_pull(*_a, **_k):
        if pull_raises:
            raise pull_raises
        rec["pull"] += 1

    monkeypatch.setattr(su, "pull", fake_pull)

    async def fake_make(target, *_a, **_k):
        rec["make"].append(target)
        if make_raises:
            raise make_raises
        return f"<output of make {target}>"

    monkeypatch.setattr(su, "run_make", fake_make)
    monkeypatch.setattr(
        su, "trigger_restart_detached",
        lambda *_a, **_k: rec.__setitem__("restart", rec["restart"] + 1),
    )
    return rec


async def _drive_to_confirm(orchestrator):
    """Run /update in a task and wait until it's awaiting restart confirmation."""
    task = asyncio.create_task(orchestrator.handle_message("/update", AUTHORIZED))
    while orchestrator._restart_confirm is None:
        await asyncio.sleep(0)
    return task


async def test_update_refuses_while_task_running(monkeypatch):
    orchestrator, runner, sent = build()
    rec = _patch_self_update(monkeypatch, compare=_compare(3))
    orchestrator._task = asyncio.create_task(asyncio.sleep(100))
    await orchestrator.handle_message("/update", AUTHORIZED)
    assert any("task is running" in m.lower() for m in sent)
    assert rec["pull"] == 0  # never even fetched/pulled
    orchestrator._task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await orchestrator._task


async def test_update_reports_already_up_to_date(monkeypatch):
    orchestrator, runner, sent = build()
    rec = _patch_self_update(monkeypatch, compare=_compare(0))
    await orchestrator.handle_message("/update", AUTHORIZED)
    assert any("up to date" in m.lower() for m in sent)
    assert rec["pull"] == 0


async def test_update_surfaces_check_failure(monkeypatch):
    orchestrator, runner, sent = build()
    rec = _patch_self_update(
        monkeypatch, compare=su.SelfUpdateError("git fetch failed:\nboom")
    )
    await orchestrator.handle_message("/update", AUTHORIZED)
    assert any("boom" in m for m in sent)
    assert rec["pull"] == 0


async def test_update_full_flow_runs_setup_config_and_restarts_on_ok(monkeypatch):
    orchestrator, runner, sent = build()
    rec = _patch_self_update(
        monkeypatch,
        compare=_compare(2, ["abc fix login", "def add feature"]),
        snapshots=[
            su.Snapshot(b"deps-v1", b"cfg-v1"),
            su.Snapshot(b"deps-v2", b"cfg-v2"),  # both changed
        ],
    )
    task = await _drive_to_confirm(orchestrator)
    await orchestrator.handle_message("ok", AUTHORIZED)
    await task
    assert rec["pull"] == 1
    assert rec["make"] == ["setup", "config"]
    assert rec["restart"] == 1
    text = "\n".join(sent)
    assert "abc fix login" in text                 # pull preview
    assert "<output of make config>" in text        # config output forwarded
    assert any("Restarting now" in m for m in sent)


async def test_update_skips_restart_on_no(monkeypatch):
    orchestrator, runner, sent = build()
    rec = _patch_self_update(
        monkeypatch,
        compare=_compare(1, ["abc fix"]),
        snapshots=[su.Snapshot(b"d", b"c"), su.Snapshot(b"d", b"c")],  # unchanged
    )
    task = await _drive_to_confirm(orchestrator)
    await orchestrator.handle_message("no", AUTHORIZED)
    await task
    assert rec["pull"] == 1
    assert rec["make"] == []           # nothing changed → no setup/config
    assert rec["restart"] == 0
    assert any("skipped" in m.lower() for m in sent)


async def test_update_runs_setup_only_when_deps_change(monkeypatch):
    orchestrator, runner, sent = build()
    rec = _patch_self_update(
        monkeypatch,
        compare=_compare(1, ["abc"]),
        snapshots=[su.Snapshot(b"d1", b"c"), su.Snapshot(b"d2", b"c")],  # deps only
    )
    task = await _drive_to_confirm(orchestrator)
    await orchestrator.handle_message("ok", AUTHORIZED)
    await task
    assert rec["make"] == ["setup"]


async def test_update_aborts_on_pull_failure(monkeypatch):
    orchestrator, runner, sent = build()
    rec = _patch_self_update(
        monkeypatch,
        compare=_compare(1, ["abc"]),
        snapshots=[su.Snapshot(b"d", b"c")],  # only the pre-pull snapshot is read
        pull_raises=su.SelfUpdateError("git pull failed:\nNot possible to fast-forward"),
    )
    await orchestrator.handle_message("/update", AUTHORIZED)
    assert any("fast-forward" in m for m in sent)
    assert rec["make"] == [] and rec["restart"] == 0
    assert orchestrator._restart_confirm is None  # never reached the confirm step


async def test_update_aborts_on_setup_failure(monkeypatch):
    orchestrator, runner, sent = build()
    rec = _patch_self_update(
        monkeypatch,
        compare=_compare(1, ["abc"]),
        snapshots=[su.Snapshot(b"d1", b"c"), su.Snapshot(b"d2", b"c")],
        make_raises=su.SelfUpdateError("make setup failed:\nError 1"),
    )
    await orchestrator.handle_message("/update", AUTHORIZED)
    assert any("Error 1" in m for m in sent)
    assert rec["restart"] == 0
    assert orchestrator._restart_confirm is None


async def test_update_aborts_on_config_failure(monkeypatch):
    orchestrator, runner, sent = build()
    rec = _patch_self_update(
        monkeypatch,
        compare=_compare(1, ["abc"]),
        snapshots=[su.Snapshot(b"d", b"c1"), su.Snapshot(b"d", b"c2")],  # config only
        make_raises=su.SelfUpdateError("make config failed:\nboom"),
    )
    await orchestrator.handle_message("/update", AUTHORIZED)
    assert rec["make"] == ["config"]  # deps unchanged → setup skipped
    assert any("boom" in m for m in sent)
    assert rec["restart"] == 0
    assert orchestrator._restart_confirm is None


async def test_update_aborts_when_pre_pull_snapshot_fails(monkeypatch):
    # snapshot() before the pull raises (e.g. config.example.yaml unreadable) —
    # abort before touching git so nothing is half-applied.
    orchestrator, runner, sent = build()
    rec = _patch_self_update(monkeypatch, compare=_compare(1, ["abc"]))

    def boom(*_a, **_k):
        raise su.SelfUpdateError("reading update snapshot failed:\nno pyproject")

    monkeypatch.setattr(su, "snapshot", boom)
    await orchestrator.handle_message("/update", AUTHORIZED)
    assert any("Update aborted" in m for m in sent)
    assert rec["pull"] == 0  # never pulled
    assert rec["make"] == [] and rec["restart"] == 0
    assert orchestrator._restart_confirm is None


async def test_update_surfaces_post_pull_snapshot_failure(monkeypatch):
    # The pull succeeds but the after-snapshot fails (e.g. the pull renamed
    # config.example.yaml) — report it and tell the user to restart manually
    # rather than silently skipping setup/config.
    orchestrator, runner, sent = build()
    rec = _patch_self_update(monkeypatch, compare=_compare(1, ["abc"]))
    calls = {"n": 0}

    def snap(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return su.Snapshot(b"d", b"c")
        raise su.SelfUpdateError("reading update snapshot failed:\nconfig renamed")

    monkeypatch.setattr(su, "snapshot", snap)
    await orchestrator.handle_message("/update", AUTHORIZED)
    assert rec["pull"] == 1  # pulled, then the after-snapshot failed
    assert any("checking deps/config failed" in m for m in sent)
    assert any("make daemon-restart" in m for m in sent)
    assert rec["make"] == [] and rec["restart"] == 0
    assert orchestrator._restart_confirm is None


async def test_update_restart_confirm_times_out(monkeypatch):
    orchestrator, runner, sent = build()
    orchestrator._config.permission_ask_timeout = 0.01
    rec = _patch_self_update(
        monkeypatch,
        compare=_compare(1, ["abc"]),
        snapshots=[su.Snapshot(b"d", b"c"), su.Snapshot(b"d", b"c")],
    )
    await orchestrator.handle_message("/update", AUTHORIZED)  # no reply → times out
    assert any("not confirmed" in m.lower() for m in sent)
    assert rec["restart"] == 0
    assert orchestrator._restart_confirm is None


async def test_plain_ok_without_pending_restart_or_permission(monkeypatch):
    # The restart-confirm path must not swallow a bare ok when nothing is
    # pending — it still falls through to the "no pending operation" reply.
    orchestrator, runner, sent = build()
    await orchestrator.handle_message("ok", AUTHORIZED)
    assert any("no pending operation" in m.lower() for m in sent)


async def test_update_restart_confirm_released_by_stop(monkeypatch):
    orchestrator, runner, sent = build()
    rec = _patch_self_update(
        monkeypatch,
        compare=_compare(1, ["abc"]),
        snapshots=[su.Snapshot(b"d", b"c"), su.Snapshot(b"d", b"c")],
    )
    task = await _drive_to_confirm(orchestrator)
    await orchestrator.handle_message("/stop", AUTHORIZED)
    await task
    assert rec["restart"] == 0
    assert orchestrator._restart_confirm is None
    assert any("skipped" in m.lower() for m in sent)


async def test_update_restart_confirm_released_by_clear(monkeypatch):
    orchestrator, runner, sent = build()
    rec = _patch_self_update(
        monkeypatch,
        compare=_compare(1, ["abc"]),
        snapshots=[su.Snapshot(b"d", b"c"), su.Snapshot(b"d", b"c")],
    )
    task = await _drive_to_confirm(orchestrator)
    await orchestrator.handle_message("/clear", AUTHORIZED)
    await task
    assert rec["restart"] == 0
    assert orchestrator._restart_confirm is None


async def test_prompt_during_update_is_refused_so_confirm_is_never_stolen(monkeypatch):
    # The core fix: while /update is in flight no new turn may start — a prompt
    # is refused with a notice. So no tool-permission reply is ever in flight to
    # collide with the restart confirm, and a later ok unambiguously restarts.
    orchestrator, runner, sent = build()
    rec = _patch_self_update(
        monkeypatch,
        compare=_compare(1, ["abc"]),
        snapshots=[su.Snapshot(b"d", b"c"), su.Snapshot(b"d", b"c")],
    )
    task = await _drive_to_confirm(orchestrator)
    sent.clear()
    await orchestrator.handle_message("do other work", AUTHORIZED)
    assert any("updating" in m.lower() for m in sent)  # refused, not started
    assert runner.turns == []  # no turn started mid-update
    assert orchestrator._restart_confirm is not None  # confirm still pending
    await orchestrator.handle_message("ok", AUTHORIZED)  # belongs to the confirm
    await task
    assert rec["restart"] == 1


async def test_prompt_during_update_git_phase_is_refused(monkeypatch):
    # The real race window: a prompt arriving while /update is still pulling —
    # before the restart confirm even exists — must be refused, not started.
    orchestrator, runner, sent = build()
    rec = _patch_self_update(
        monkeypatch,
        compare=_compare(1, ["abc"]),
        snapshots=[su.Snapshot(b"d", b"c"), su.Snapshot(b"d", b"c")],
    )
    gate = asyncio.Event()

    async def blocking_pull(*_a, **_k):
        await gate.wait()
        rec["pull"] += 1

    monkeypatch.setattr(su, "pull", blocking_pull)
    task = asyncio.create_task(orchestrator.handle_message("/update", AUTHORIZED))
    while not any("Pulling" in m for m in sent):
        await asyncio.sleep(0)
    assert orchestrator._updating is True
    assert orchestrator._restart_confirm is None  # confirm not created yet
    await orchestrator.handle_message("do other work", AUTHORIZED)
    assert any("updating" in m.lower() for m in sent)  # refused
    assert runner.turns == []  # no turn started
    gate.set()  # let the pull and the rest of the update proceed
    while orchestrator._restart_confirm is None:
        await asyncio.sleep(0)
    await orchestrator.handle_message("no", AUTHORIZED)  # decline the restart
    await task
    assert rec["restart"] == 0


async def test_voice_message_during_update_is_refused(monkeypatch):
    # handle_audio funnels into _cmd_prompt, so a voice message mid-update is
    # refused just like a typed one — no turn starts, the confirm stays pending.
    orchestrator, runner, sent = build()
    rec = _patch_self_update(
        monkeypatch,
        compare=_compare(1, ["abc"]),
        snapshots=[su.Snapshot(b"d", b"c"), su.Snapshot(b"d", b"c")],
    )
    task = await _drive_to_confirm(orchestrator)
    sent.clear()
    await orchestrator.handle_audio("run the tests", AUTHORIZED)
    assert any("updating" in m.lower() for m in sent)
    assert runner.turns == []
    assert orchestrator._restart_confirm is not None
    await orchestrator.handle_message("no", AUTHORIZED)
    await task
    assert rec["restart"] == 0


async def test_image_message_during_update_is_refused(monkeypatch):
    # handle_image likewise funnels into _cmd_prompt and is refused mid-update.
    orchestrator, runner, sent = build()
    rec = _patch_self_update(
        monkeypatch,
        compare=_compare(1, ["abc"]),
        snapshots=[su.Snapshot(b"d", b"c"), su.Snapshot(b"d", b"c")],
    )
    task = await _drive_to_confirm(orchestrator)
    sent.clear()
    await orchestrator.handle_image("describe [image]", AUTHORIZED)
    assert any("updating" in m.lower() for m in sent)
    assert runner.turns == []
    await orchestrator.handle_message("no", AUTHORIZED)
    await task
    assert rec["restart"] == 0


async def test_second_update_refused_while_confirm_pending(monkeypatch):
    orchestrator, runner, sent = build()
    rec = _patch_self_update(
        monkeypatch,
        compare=_compare(1, ["abc"]),
        snapshots=[su.Snapshot(b"d", b"c"), su.Snapshot(b"d", b"c")],
    )
    task = await _drive_to_confirm(orchestrator)
    sent.clear()
    await orchestrator.handle_message("/update", AUTHORIZED)  # second, concurrent
    assert any("already in progress" in m.lower() for m in sent)
    assert rec["pull"] == 1  # the second never pulled again
    await orchestrator.handle_message("no", AUTHORIZED)  # resolve the first
    await task
    assert rec["restart"] == 0
