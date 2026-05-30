import json

from claude_dingtalk_bridge.claude_runner import ClaudeRunner, tool_summary
from claude_dingtalk_bridge.config import PermissionRules


def test_tool_summary_for_bash():
    assert tool_summary("Bash", {"command": "go test ./..."}) == "go test ./..."


def test_tool_summary_for_file_tool():
    assert tool_summary("Edit", {"file_path": "/tmp/a.py"}) == "/tmp/a.py"


def test_tool_summary_for_grep():
    assert tool_summary("Grep", {"pattern": "TODO"}) == "TODO"


def test_tool_summary_for_grep_pattern_in_path():
    # Both pattern and path set: `pattern in path` reads naturally and
    # surfaces both. The generic fallback would let `path` shadow `pattern`,
    # hiding what's being searched for — backwards from a debugging POV.
    assert (
        tool_summary("Grep", {"pattern": "TODO", "path": "src"})
        == "TODO in src"
    )


def test_tool_summary_for_grep_path_only():
    # Pattern-less grep is degenerate; fall back to the path so the line is
    # not blank.
    assert tool_summary("Grep", {"path": "src"}) == "src"


def test_tool_summary_empty_when_no_fields():
    assert tool_summary("WebFetch", {}) == ""


def test_build_options_sets_cwd():
    runner = ClaudeRunner()
    options = runner._build_options("/tmp/proj")
    assert options.cwd == "/tmp/proj"


def test_format_elapsed_sub_second_renders_ms():
    from claude_dingtalk_bridge.claude_runner import _format_elapsed
    assert _format_elapsed(0.42) == "420ms"


def test_format_elapsed_one_second_or_more_renders_seconds():
    from claude_dingtalk_bridge.claude_runner import _format_elapsed
    assert _format_elapsed(4.25) == "4.2s"
    assert _format_elapsed(1.0) == "1.0s"


def test_format_unix_ts_returns_none_for_none():
    from claude_dingtalk_bridge.claude_runner import _format_unix_ts
    assert _format_unix_ts(None) is None


def test_format_unix_ts_falls_back_to_raw_on_bad_value():
    # An out-of-range timestamp (Y10K+) raises OSError/OverflowError on
    # fromtimestamp. The renderer degrades to the raw integer rather than
    # crashing the log line.
    from claude_dingtalk_bridge.claude_runner import _format_unix_ts
    huge = 10**15
    assert _format_unix_ts(huge) == str(huge)


def test_build_options_omits_resume_when_no_session():
    runner = ClaudeRunner()
    options = runner._build_options("/tmp/proj")
    assert getattr(options, "resume", None) is None


def test_build_options_passes_resume_when_session_known():
    runner = ClaudeRunner()
    runner._session_ids["/tmp/proj"] = "sess-abc"
    options = runner._build_options("/tmp/proj")
    assert options.resume == "sess-abc"


def test_reset_clears_session():
    runner = ClaudeRunner()
    runner._session_ids["/tmp/proj"] = "sess-abc"
    runner.reset("/tmp/proj")
    assert "/tmp/proj" not in runner._session_ids


def test_next_turn_increments_per_project():
    runner = ClaudeRunner()
    assert runner.next_turn("/a") == 1
    assert runner.next_turn("/a") == 2
    assert runner.next_turn("/b") == 1
    assert runner.next_turn("/a") == 3


def test_reset_restarts_turn_count():
    runner = ClaudeRunner()
    runner.next_turn("/a")
    runner.next_turn("/a")
    runner.reset("/a")
    assert runner.next_turn("/a") == 1


def test_set_session_restarts_turn_count():
    # Switching sessions starts a fresh conversation; the turn counter resets
    # alongside the token tally so log labels track the new session cleanly.
    runner = ClaudeRunner()
    runner.next_turn("/a")
    runner.set_session("/a", "new-sess")
    assert runner.next_turn("/a") == 1


def test_tool_summary_for_url():
    assert tool_summary("WebFetch", {"url": "https://example.com"}) == "https://example.com"


def test_tool_summary_for_skill():
    assert tool_summary("Skill", {"skill": "superpowers:brainstorming"}) == "superpowers:brainstorming"


def test_tool_summary_for_agent_prefers_description():
    # Agent/Task carry both subagent_type and description; description is the
    # more identifying field (what the subagent will actually do), so prefer it.
    out = tool_summary("Agent", {"subagent_type": "general-purpose", "description": "Code quality review"})
    assert out == "Code quality review"


def test_tool_summary_for_agent_falls_back_to_subagent_type():
    out = tool_summary("Task", {"subagent_type": "Explore"})
    assert out == "Explore"


def test_tool_summary_for_tool_search():
    assert tool_summary("ToolSearch", {"query": "scheduling"}) == "scheduling"


def test_tool_summary_for_task_get_accepts_both_id_casings():
    # Real SDK is inconsistent: TaskGet uses `taskId` (camelCase), TaskOutput
    # uses `task_id` (snake_case). Accept both so neither renders blank.
    assert tool_summary("TaskGet", {"taskId": "ad14d05ba8eebd333"}) == "ad14d05ba8eebd333"
    assert tool_summary("TaskOutput", {"task_id": "abc"}) == "abc"
    assert tool_summary("TaskStop", {"taskId": "xyz"}) == "xyz"


def test_tool_summary_for_task_create_uses_subject():
    # Real SDK shape: {"subject": "#8 …", "description": "Change …"} —
    # subject is the short title the operator actually wants to see.
    out = tool_summary("TaskCreate", {
        "subject": "#8 geo cache TTL 60→30",
        "description": "Change GEO_CACHE_TTL_SECONDS to 30.0 in geo.py",
    })
    assert out == "#8 geo cache TTL 60→30"


def test_tool_summary_for_task_create_falls_back_to_description():
    # If subject is missing for some reason, fall back to description rather
    # than rendering blank.
    out = tool_summary("TaskCreate", {"description": "Do the thing"})
    assert out == "Do the thing"


def test_tool_summary_for_task_update_renders_state_transition():
    # `{taskId, status}` → `"<id> → <status>"` — reads like an action.
    out = tool_summary("TaskUpdate", {"taskId": "1", "status": "in_progress"})
    assert out == "1 → in_progress"


def test_tool_summary_for_task_update_handles_partial_input():
    # Just an id (no status), or just status — still surface what we have
    # rather than render blank.
    assert tool_summary("TaskUpdate", {"taskId": "1"}) == "1"
    assert tool_summary("TaskUpdate", {"status": "completed"}) == "completed"


def test_tool_summary_for_task_list_is_empty():
    # TaskList takes no arguments — empty summary means we render just the
    # tool name without the `(…)` suffix.
    assert tool_summary("TaskList", {}) == ""


def test_tool_summary_for_ask_user_question_single():
    out = tool_summary("AskUserQuestion", {"questions": [{"header": "DB", "question": "Which database?"}]})
    assert out == "Which database?"


def test_tool_summary_for_ask_user_question_multiple_shows_count():
    out = tool_summary(
        "AskUserQuestion",
        {"questions": [
            {"question": "Which DB?"},
            {"question": "Which cache?"},
            {"question": "Which queue?"},
        ]},
    )
    assert out == "Which DB? (×3)"


def test_tool_summary_for_ask_user_question_empty_list():
    # Degenerate input — empty questions list. Returns empty so the tool
    # entry renders as bare `AskUserQuestion#<id>` rather than crashing.
    assert tool_summary("AskUserQuestion", {"questions": []}) == ""
    assert tool_summary("AskUserQuestion", {}) == ""


def test_tool_summary_collapses_project_path_in_file_tool():
    # Regression: ToolEvent (phone) and request_permission both call
    # tool_summary directly; before, the phone got the full
    # /Users/.../proj/src/x.py while only the log was collapsed. Now
    # tool_summary itself returns collapsed paths so every caller is
    # consistent.
    from claude_dingtalk_bridge import log_context
    log_context.set_cwd("/Users/dev/proj")
    try:
        out = tool_summary("Read", {"file_path": "/Users/dev/proj/src/a.py"})
        assert out == "src/a.py"
    finally:
        log_context.set_cwd("")


def test_tool_summary_collapses_paths_inside_bash_command():
    # Bash commands embed paths anywhere — the substitution still has to fire.
    from claude_dingtalk_bridge import log_context
    log_context.set_cwd("/Users/dev/proj")
    try:
        out = tool_summary("Bash", {"command": "ls /Users/dev/proj/src && cat /tmp/x"})
        assert out == "ls src && cat /tmp/x"
    finally:
        log_context.set_cwd("")


def test_tool_summary_collapses_paths_inside_grep_pattern_in_path():
    # The Grep "pattern in path" format passes through collapse — the path
    # half gets shortened, the pattern (free text) is untouched.
    from claude_dingtalk_bridge import log_context
    log_context.set_cwd("/Users/dev/proj")
    try:
        out = tool_summary("Grep", {"pattern": "TODO", "path": "/Users/dev/proj/src"})
        assert out == "TODO in src"
    finally:
        log_context.set_cwd("")


def test_build_options_enables_1h_cache_without_proxy():
    runner = ClaudeRunner()
    options = runner._build_options("/tmp/proj")
    assert options.env == {
        "ENABLE_PROMPT_CACHING_1H": "1",
        "CLAUDE_CODE_ENTRYPOINT": "claude-dingtalk-bridge",
    }


def test_build_options_injects_proxy_env_alongside_cache():
    runner = ClaudeRunner()
    runner.proxy_url = "http://127.0.0.1:8118"
    options = runner._build_options("/tmp/proj")
    assert options.env == {
        "http_proxy": "http://127.0.0.1:8118",
        "https_proxy": "http://127.0.0.1:8118",
        "HTTP_PROXY": "http://127.0.0.1:8118",
        "HTTPS_PROXY": "http://127.0.0.1:8118",
        "ENABLE_PROMPT_CACHING_1H": "1",
        "CLAUDE_CODE_ENTRYPOINT": "claude-dingtalk-bridge",
    }


def test_build_options_excludes_dynamic_sections():
    runner = ClaudeRunner()
    options = runner._build_options("/tmp/proj")
    assert options.system_prompt == {
        "type": "preset",
        "preset": "claude_code",
        "exclude_dynamic_sections": True,
    }


def test_set_and_get_session():
    from claude_dingtalk_bridge.claude_runner import ClaudeRunner

    runner = ClaudeRunner()
    assert runner.current_session("/tmp/proj") is None
    runner.set_session("/tmp/proj", "the-session-id")
    assert runner.current_session("/tmp/proj") == "the-session-id"


def test_reset_clears_set_session():
    from claude_dingtalk_bridge.claude_runner import ClaudeRunner

    runner = ClaudeRunner()
    runner.set_session("/tmp/proj", "the-session-id")
    runner.reset("/tmp/proj")
    assert runner.current_session("/tmp/proj") is None


def test_set_session_flows_into_resume_option():
    from claude_dingtalk_bridge.claude_runner import ClaudeRunner

    runner = ClaudeRunner()
    assert runner._build_options("/tmp/proj").resume is None
    runner.set_session("/tmp/proj", "the-session-id")
    assert runner._build_options("/tmp/proj").resume == "the-session-id"


def test_log_cache_usage_reports_1h_writes(caplog):
    import logging

    from claude_dingtalk_bridge.claude_runner import _log_cache_usage

    usage = {
        "input_tokens": 12,
        "output_tokens": 34,
        "cache_read_input_tokens": 5000,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 8000,
            "ephemeral_5m_input_tokens": 0,
        },
    }
    with caplog.at_level(logging.INFO):
        _log_cache_usage(usage)
    # Token counts >= 1000 abbreviate to ``NK`` so the line stays scannable
    # at typical turn sizes (35K cache_read vs 35500 reads at a glance).
    assert "write_1h=8K" in caplog.text
    assert "write_5m=0" in caplog.text
    assert "cache_read=5K" in caplog.text
    assert "input=12" in caplog.text
    assert "output=34" in caplog.text
    # hit = read / (read + write_1h + write_5m + input) = 5000 / 13012 = 38.4%
    assert "hit=38.4%" in caplog.text


def test_log_cache_usage_hit_handles_zero_prompt_tokens(caplog):
    # Pathological case (no prompt tokens at all) — hit is undefined; report
    # as `n/a` instead of dividing by zero.
    import logging
    from claude_dingtalk_bridge.claude_runner import _log_cache_usage
    with caplog.at_level(logging.INFO):
        _log_cache_usage({"output_tokens": 5})
    assert "hit=n/a" in caplog.text


def test_log_cache_usage_ignores_empty(caplog):
    import logging

    from claude_dingtalk_bridge.claude_runner import _log_cache_usage

    with caplog.at_level(logging.INFO):
        _log_cache_usage(None)
    assert caplog.text == ""


def test_record_usage_accumulates_session_tokens():
    runner = ClaudeRunner()
    runner.record_usage("/p", {
        "input_tokens": 100, "output_tokens": 50,
        "cache_read_input_tokens": 800, "cache_creation_input_tokens": 200,
    })
    assert runner.session_tokens("/p") == 1150
    runner.record_usage("/p", {"input_tokens": 10, "output_tokens": 5})
    assert runner.session_tokens("/p") == 1165


def test_record_usage_stores_last_usage():
    runner = ClaudeRunner()
    usage = {"input_tokens": 1, "cache_read_input_tokens": 9}
    runner.record_usage("/p", usage)
    assert runner.last_usage("/p") == usage


def test_session_tokens_defaults_to_zero():
    runner = ClaudeRunner()
    assert runner.session_tokens("/p") == 0
    assert runner.last_usage("/p") is None


def test_reset_clears_token_tally():
    runner = ClaudeRunner()
    runner.record_usage("/p", {"input_tokens": 100})
    runner.reset("/p")
    assert runner.session_tokens("/p") == 0
    assert runner.last_usage("/p") is None


def test_set_session_resets_token_tally():
    runner = ClaudeRunner()
    runner.record_usage("/p", {"input_tokens": 100})
    runner.set_session("/p", "new-id")
    assert runner.session_tokens("/p") == 0


def test_record_usage_accumulates_per_model_tokens():
    runner = ClaudeRunner()
    runner.record_usage(
        "/p",
        {
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_input_tokens": 800, "cache_creation_input_tokens": 200,
        },
        model_usage={
            "claude-opus-4-7": {
                "inputTokens": 70, "outputTokens": 40,
                "cacheReadInputTokens": 800, "cacheCreationInputTokens": 200,
            },
            "claude-haiku-4-5": {
                "inputTokens": 30, "outputTokens": 10,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
            },
        },
    )
    assert runner.session_model_tokens("/p") == {
        "claude-opus-4-7": 1110,
        "claude-haiku-4-5": 40,
    }
    runner.record_usage(
        "/p", {"input_tokens": 10},
        model_usage={
            "claude-opus-4-7": {
                "inputTokens": 10, "outputTokens": 0,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
            },
        },
    )
    assert runner.session_model_tokens("/p") == {
        "claude-opus-4-7": 1120,
        "claude-haiku-4-5": 40,
    }


def test_record_usage_session_total_sums_model_usage_when_present():
    # usage carries only the main agent's totals; model_usage is the only
    # source for subagent tokens. So session_tokens must follow model_usage
    # when it's available, otherwise subagent cost vanishes from /status.
    runner = ClaudeRunner()
    runner.record_usage(
        "/p",
        {"input_tokens": 7, "output_tokens": 200,
         "cache_read_input_tokens": 50000, "cache_creation_input_tokens": 7000},
        model_usage={
            "claude-opus-4-7": {
                "inputTokens": 7, "outputTokens": 200,
                "cacheReadInputTokens": 50000, "cacheCreationInputTokens": 7000,
            },
            "claude-haiku-4-5": {
                "inputTokens": 3, "outputTokens": 4,
                "cacheReadInputTokens": 20000, "cacheCreationInputTokens": 2000,
            },
        },
    )
    assert runner.session_tokens("/p") == 57207 + 22007


def test_record_usage_stores_last_model_usage():
    runner = ClaudeRunner()
    mu = {"claude-opus-4-7": {"inputTokens": 1, "outputTokens": 0,
        "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}
    runner.record_usage("/p", {"input_tokens": 1}, model_usage=mu)
    assert runner.last_model_usage("/p") == mu


def test_session_model_tokens_defaults_to_empty():
    runner = ClaudeRunner()
    assert runner.session_model_tokens("/p") == {}
    assert runner.last_model_usage("/p") is None


def test_record_usage_without_model_usage_leaves_per_model_empty():
    runner = ClaudeRunner()
    runner.record_usage("/p", {"input_tokens": 100})
    assert runner.session_tokens("/p") == 100
    assert runner.session_model_tokens("/p") == {}
    assert runner.last_model_usage("/p") is None


def test_record_usage_tracks_cost():
    runner = ClaudeRunner()
    runner.record_usage("/p", {"input_tokens": 1}, cost_usd=0.5)
    assert runner.session_cost("/p") == 0.5
    assert runner.last_turn_cost("/p") == 0.5
    runner.record_usage("/p", {"input_tokens": 2}, cost_usd=1.5)
    assert runner.session_cost("/p") == 2.0
    assert runner.last_turn_cost("/p") == 1.5


def test_session_cost_defaults_to_zero():
    runner = ClaudeRunner()
    assert runner.session_cost("/p") == 0.0
    assert runner.last_turn_cost("/p") is None


def test_record_usage_with_no_cost_leaves_totals_alone():
    # SDK occasionally omits total_cost_usd (None). The session running tally
    # must not break; we just don't have a number for that turn.
    runner = ClaudeRunner()
    runner.record_usage("/p", {"input_tokens": 1}, cost_usd=0.25)
    runner.record_usage("/p", {"input_tokens": 2}, cost_usd=None)
    assert runner.session_cost("/p") == 0.25
    assert runner.last_turn_cost("/p") is None


def test_reset_clears_cost_tally():
    runner = ClaudeRunner()
    runner.record_usage("/p", {"input_tokens": 1}, cost_usd=0.5)
    runner.reset("/p")
    assert runner.session_cost("/p") == 0.0
    assert runner.last_turn_cost("/p") is None


def test_set_session_clears_cost_tally():
    runner = ClaudeRunner()
    runner.record_usage("/p", {"input_tokens": 1}, cost_usd=0.5)
    runner.set_session("/p", "new-id")
    assert runner.session_cost("/p") == 0.0
    assert runner.last_turn_cost("/p") is None


def test_record_usage_clears_stale_last_model_usage():
    # turn N carries model_usage, turn N+1 does not (SDK occasionally omits
    # the field). last_model_usage must reflect turn N+1 — otherwise /status
    # shows the prior turn's per-model breakdown as if it were the latest.
    runner = ClaudeRunner()
    runner.record_usage(
        "/p", {"input_tokens": 1},
        model_usage={"claude-opus-4-7": {
            "inputTokens": 1, "outputTokens": 0,
            "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
        }},
    )
    assert runner.last_model_usage("/p") is not None
    runner.record_usage("/p", {"input_tokens": 2})
    assert runner.last_model_usage("/p") is None


def test_reset_clears_per_model_state():
    runner = ClaudeRunner()
    runner.record_usage(
        "/p", {"input_tokens": 100},
        model_usage={"claude-opus-4-7": {
            "inputTokens": 100, "outputTokens": 0,
            "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
        }},
    )
    runner.reset("/p")
    assert runner.session_model_tokens("/p") == {}
    assert runner.last_model_usage("/p") is None


def test_set_session_clears_per_model_state():
    runner = ClaudeRunner()
    runner.record_usage(
        "/p", {"input_tokens": 100},
        model_usage={"claude-opus-4-7": {
            "inputTokens": 100, "outputTokens": 0,
            "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
        }},
    )
    runner.set_session("/p", "new-id")
    assert runner.session_model_tokens("/p") == {}
    assert runner.last_model_usage("/p") is None


def test_translate_compact_boundary_emits_text():
    from claude_agent_sdk import SystemMessage

    from claude_dingtalk_bridge.claude_runner import TextEvent, _translate

    msg = SystemMessage(
        subtype="compact_boundary",
        data={"compact_metadata": {"pre_tokens": 45000, "trigger": "manual"}},
    )
    events = _translate(msg)
    assert len(events) == 1
    assert isinstance(events[0], TextEvent)
    assert "45000" in events[0].text


def test_translate_compact_boundary_without_pre_tokens():
    from claude_agent_sdk import SystemMessage

    from claude_dingtalk_bridge.claude_runner import TextEvent, _translate

    msg = SystemMessage(subtype="compact_boundary", data={})
    events = _translate(msg)
    assert len(events) == 1
    assert isinstance(events[0], TextEvent)
    assert "compacted" in events[0].text.lower()


def test_model_override_and_observed_default_none():
    runner = ClaudeRunner()
    assert runner.model_override is None
    assert runner.observed_model is None


def test_set_model_updates_override():
    runner = ClaudeRunner()
    runner.set_model("opus")
    assert runner.model_override == "opus"


def test_build_options_omits_model_by_default():
    runner = ClaudeRunner()
    options = runner._build_options("/tmp/proj")
    assert getattr(options, "model", None) is None


def test_build_options_passes_model_when_set():
    runner = ClaudeRunner()
    runner.set_model("sonnet")
    options = runner._build_options("/tmp/proj")
    assert options.model == "sonnet"


def test_permission_mode_default_none():
    runner = ClaudeRunner()
    assert runner.permission_mode is None


def test_set_permission_mode_updates_override():
    runner = ClaudeRunner()
    runner.set_permission_mode("acceptEdits")
    assert runner.permission_mode == "acceptEdits"
    runner.set_permission_mode(None)
    assert runner.permission_mode is None


def test_build_options_omits_permission_mode_by_default():
    runner = ClaudeRunner()
    options = runner._build_options("/tmp/proj")
    # When unset, leaving the flag off lets the settings layer's defaultMode
    # (if any) apply.
    assert getattr(options, "permission_mode", None) is None


def test_build_options_passes_permission_mode_when_set():
    runner = ClaudeRunner()
    runner.set_permission_mode("plan")
    options = runner._build_options("/tmp/proj")
    assert options.permission_mode == "plan"


async def test_note_system_message_captures_model():
    from claude_agent_sdk import SystemMessage

    runner = ClaudeRunner()
    init = SystemMessage(subtype="init", data={"model": "claude-opus-4-7"})
    runner._note_system_message(init, "/tmp/proj")
    assert runner.observed_model == "claude-opus-4-7"


async def test_note_system_message_caches_session_id_for_callback_lookup():
    # Cached session id lets the SDK's can_use_tool callback (which runs in
    # a forked task that can't see contextvar updates) restamp log_context
    # before invoking the orchestrator's handlers — without this cache,
    # turn 1 of a brand-new project tags ask_user_question / permission
    # log lines with `session=-`.
    from claude_agent_sdk import SystemMessage

    runner = ClaudeRunner()
    init = SystemMessage(
        subtype="init",
        data={"model": "opus", "session_id": "03cdbc4a-1234"},
    )
    runner._note_system_message(init, "/tmp/proj")
    assert runner.current_session("/tmp/proj") == "03cdbc4a-1234"


def test_note_system_message_ignores_non_init():
    from claude_agent_sdk import SystemMessage

    runner = ClaudeRunner()
    runner._note_system_message(SystemMessage(subtype="other", data={"model": "x"}), "/tmp/proj")
    assert runner.observed_model is None


def test_translate_todo_write_emits_todo_event():
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    from claude_dingtalk_bridge.claude_runner import TodoEvent, _translate

    block = ToolUseBlock(
        id="t1",
        name="TodoWrite",
        input={
            "todos": [
                {"content": "Fix bug", "status": "completed", "activeForm": "Fixing bug"},
                {"content": "Add tests", "status": "in_progress", "activeForm": "Adding tests"},
            ]
        },
    )
    msg = AssistantMessage(content=[block], model="opus")
    events = _translate(msg)
    assert len(events) == 1
    assert isinstance(events[0], TodoEvent)
    assert events[0].items == [
        ("Fix bug", "completed", "Fixing bug"),
        ("Add tests", "in_progress", "Adding tests"),
    ]


def test_translate_non_todo_tool_still_emits_tool_event():
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    from claude_dingtalk_bridge.claude_runner import ToolEvent, _translate

    block = ToolUseBlock(id="t2", name="Bash", input={"command": "ls"})
    events = _translate(AssistantMessage(content=[block], model="opus"))
    assert len(events) == 1
    assert isinstance(events[0], ToolEvent)
    assert events[0].name == "Bash"


def test_translate_subagent_task_started_emits_event_and_tracks_id():
    # A real subagent task carries subagent_type — it surfaces and its id is
    # recorded so the later notification (which carries no subagent_type) can
    # still be recognised as a subagent's.
    from claude_agent_sdk import TaskStartedMessage

    from claude_dingtalk_bridge.claude_runner import TaskEvent, _translate

    msg = TaskStartedMessage(
        subtype="task_started",
        data={"subagent_type": "general-purpose"},
        task_id="task-1",
        description="Fix Task 1",
        uuid="u1",
        session_id="s1",
    )
    subagents: dict[str, str] = {}
    events = _translate(msg, subagents)
    assert len(events) == 1
    assert isinstance(events[0], TaskEvent)
    assert events[0].phase == "started"
    assert events[0].task_id == "task-1"
    assert events[0].description == "Fix Task 1"
    assert subagents.get("task-1") == "Fix Task 1"


def test_translate_non_subagent_task_started_is_dropped():
    # A plain Bash command also arrives as task_started but carries no
    # subagent_type — it must not be surfaced as a "Subagent" nor tracked.
    from claude_agent_sdk import TaskStartedMessage

    from claude_dingtalk_bridge.claude_runner import _translate

    msg = TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id="t-bash",
        description="Push main to origin",
        uuid="u1",
        session_id="s1",
    )
    subagents: dict[str, str] = {}
    assert _translate(msg, subagents) == []
    assert "t-bash" not in subagents


def test_translate_task_notification_carries_usage_and_description():
    # The completion event must surface SDK-reported duration/tokens AND the
    # description that only task_started carried (looked up via the tracking
    # dict).
    from claude_agent_sdk import TaskNotificationMessage

    from claude_dingtalk_bridge.claude_runner import TaskEvent, _translate

    msg = TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id="task-1",
        status="completed",
        output_file="/tmp/o",
        summary="peanut",
        uuid="u1",
        session_id="s1",
        usage={"total_tokens": 16641, "tool_uses": 0, "duration_ms": 3005},
    )
    subagents = {"task-1": "Guess riddle A"}
    events = _translate(msg, subagents)
    assert isinstance(events[0], TaskEvent)
    assert events[0].duration_ms == 3005
    assert events[0].total_tokens == 16641
    assert events[0].description == "Guess riddle A"


def test_translate_subagent_task_progress_carries_last_tool():
    from claude_agent_sdk import TaskProgressMessage

    from claude_dingtalk_bridge.claude_runner import TaskEvent, _translate

    msg = TaskProgressMessage(
        subtype="task_progress",
        data={"subagent_type": "general-purpose"},
        task_id="task-1",
        description="Fix Task 1",
        usage={},
        uuid="u1",
        session_id="s1",
        last_tool_name="Edit",
    )
    events = _translate(msg, {})
    assert isinstance(events[0], TaskEvent)
    assert events[0].phase == "progress"
    assert events[0].last_tool == "Edit"


def test_translate_non_subagent_task_progress_is_dropped():
    from claude_agent_sdk import TaskProgressMessage

    from claude_dingtalk_bridge.claude_runner import _translate

    msg = TaskProgressMessage(
        subtype="task_progress",
        data={},
        task_id="t-bash",
        description="Push main to origin",
        usage={},
        uuid="u1",
        session_id="s1",
        last_tool_name="Bash",
    )
    assert _translate(msg, {}) == []


def test_translate_task_notification_emitted_for_tracked_subagent():
    # task_notification carries no subagent_type, so a subagent's notification
    # is recognised only by its id being in the tracked set.
    from claude_agent_sdk import TaskNotificationMessage

    from claude_dingtalk_bridge.claude_runner import TaskEvent, _translate

    msg = TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id="task-1",
        status="completed",
        output_file="/tmp/out.txt",
        summary="Task 1 fixed",
        uuid="u1",
        session_id="s1",
    )
    subagents = {"task-1": "Fix Task 1"}
    events = _translate(msg, subagents)
    assert isinstance(events[0], TaskEvent)
    assert events[0].phase == "notification"
    assert events[0].status == "completed"
    assert events[0].summary == "Task 1 fixed"
    assert events[0].description == "Fix Task 1"
    assert "task-1" not in subagents


def test_translate_task_notification_dropped_for_untracked_task():
    from claude_agent_sdk import TaskNotificationMessage

    from claude_dingtalk_bridge.claude_runner import _translate

    msg = TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id="t-bash",
        status="completed",
        output_file="",
        summary="late",
        uuid="u1",
        session_id="s1",
    )
    assert _translate(msg, {}) == []


import logging as _logging  # noqa: E402

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    HookEventMessage,
    MirrorErrorMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from claude_dingtalk_bridge.claude_runner import (  # noqa: E402
    _log_sdk_message,
    _sdk_message_summary,
)


def test_summary_assistant_message_pairs_tool_use_with_id_prefix():
    # Tool-use id strips the constant `toolu_` prefix so the remaining 8 chars
    # actually disambiguate parallel calls (otherwise every id froze at
    # `toolu_01`). Tool input is surfaced via tool_summary so e.g. the Bash
    # command shows up next to the tool name.
    msg = AssistantMessage(
        content=[
            ToolUseBlock(id="toolu_abc12345xyz", name="Bash", input={"command": "ls"}),
            TextBlock(text="hello"),
            ThinkingBlock(thinking="...", signature="sig"),
        ],
        model="claude-opus-4-7",
        stop_reason="tool_use",
    )
    assert _sdk_message_summary(msg) == (
        "tools=[Bash#abc12345(ls)] text_len=5 text_preview=\"hello\" "
        "thinking_len=3 model=claude-opus-4-7 stop_reason=tool_use"
    )


def test_summary_assistant_message_parallel_tools_have_distinct_ids():
    # Pre-fix bug: `b.id[:8]` rendered every entry as `toolu_01`. The fix
    # strips the constant prefix so two parallel calls render differently.
    msg = AssistantMessage(
        content=[
            ToolUseBlock(id="toolu_01ABCDEF", name="Bash", input={"command": "git status"}),
            ToolUseBlock(id="toolu_01ZZZZZZ", name="Bash", input={"command": "git diff"}),
        ],
        model="opus",
    )
    summary = _sdk_message_summary(msg)
    assert "Bash#01ABCDEF(git status)" in summary
    assert "Bash#01ZZZZZZ(git diff)" in summary


def test_summary_assistant_message_tool_without_summary_omits_parens():
    # TodoWrite has no obvious single-field summary; tool_summary() returns ""
    # in that case. The tool entry should still render, just without parens.
    msg = AssistantMessage(
        content=[ToolUseBlock(id="toolu_01ABCDEF", name="WebFetch", input={})],
        model="opus",
    )
    assert "tools=[WebFetch#01ABCDEF]" in _sdk_message_summary(msg)


def test_summary_assistant_message_tool_input_truncated_at_80_chars():
    long_cmd = "echo " + "x" * 200
    msg = AssistantMessage(
        content=[ToolUseBlock(id="toolu_01ABCDEF", name="Bash", input={"command": long_cmd})],
        model="opus",
    )
    summary = _sdk_message_summary(msg)
    assert "…" in summary  # truncation marker
    assert "x" * 200 not in summary  # full long string not present


def _summarize_with_cwd(msg, cwd):
    """Run _sdk_message_summary under a fresh copy of the current context
    with log_context.cwd preset — isolates the cwd contextvar so tests
    don't leak into each other."""
    import contextvars
    from claude_dingtalk_bridge import log_context
    def _inner():
        log_context.set_cwd(cwd)
        return _sdk_message_summary(msg)
    return contextvars.copy_context().run(_inner)


def test_summary_assistant_message_tool_input_collapses_project_path():
    # When log_context.cwd is set, paths inside it render relative — keeps
    # `Read#abc(src/daemon.py)` from blowing up into the full project path
    # repeated on every line.
    msg = AssistantMessage(
        content=[ToolUseBlock(
            id="toolu_01ABCDEF", name="Read",
            input={"file_path": "/Users/foo/proj/src/daemon.py"},
        )],
        model="opus",
    )
    summary = _summarize_with_cwd(msg, "/Users/foo/proj")
    assert "(src/daemon.py)" in summary
    assert "/Users/foo/proj/src" not in summary


def test_summary_assistant_message_tool_input_collapses_home():
    # Paths under $HOME (but outside the project) render with `~/` prefix.
    import os
    home = os.path.expanduser("~")
    msg = AssistantMessage(
        content=[ToolUseBlock(
            id="toolu_01ABCDEF", name="Read",
            input={"file_path": f"{home}/Documents/notes.md"},
        )],
        model="opus",
    )
    summary = _summarize_with_cwd(msg, "/Users/foo/proj")
    assert "(~/Documents/notes.md)" in summary


def test_summary_assistant_message_tool_input_leaves_external_paths_alone():
    # Paths outside both project and home stay absolute — `/etc/hosts`
    # shouldn't be silently rewritten.
    msg = AssistantMessage(
        content=[ToolUseBlock(
            id="toolu_01ABCDEF", name="Read",
            input={"file_path": "/etc/hosts"},
        )],
        model="opus",
    )
    summary = _summarize_with_cwd(msg, "/Users/foo/proj")
    assert "(/etc/hosts)" in summary


def test_summary_assistant_message_collapses_cwd_inside_bash_command():
    # Bash commands often embed cwd somewhere mid-string (e.g.
    # `find /proj -name '*.py'`) — collapsing operates on the whole string,
    # not just simple file_path values.
    msg = AssistantMessage(
        content=[ToolUseBlock(
            id="toolu_01ABCDEF", name="Bash",
            input={"command": "find /Users/foo/proj -name '*.py'"},
        )],
        model="opus",
    )
    summary = _summarize_with_cwd(msg, "/Users/foo/proj")
    assert "find . -name" in summary


def test_middle_elide_preserves_head_and_tail():
    from claude_dingtalk_bridge.claude_runner import _middle_elide
    # Real-case path that previously got chopped mid-uuid losing the filename.
    s = "~/.claude/projects/-Users-yinyue-Projects-claude-dingtalk-bridge/cdbbe6c7-b912-4153-beda-1e8756d18063.jsonl"
    out = _middle_elide(s, 80)
    assert len(out) == 80
    # Both ends must survive: the dir prefix (so we know which project) and
    # the filename suffix (so we know which file).
    assert out.startswith("~/.claude/projects/")
    assert out.endswith(".jsonl")
    assert "…" in out


def test_middle_elide_passes_short_strings_through_unchanged():
    from claude_dingtalk_bridge.claude_runner import _middle_elide
    assert _middle_elide("short", 80) == "short"


def test_summary_assistant_message_uses_middle_elide_for_read_path():
    # Read on a deep path: filename must survive the truncation.
    msg = AssistantMessage(
        content=[ToolUseBlock(
            id="toolu_01AD3dZD", name="Read",
            input={"file_path": "/Users/foo/proj/very/deeply/nested/directory/with/lots/of/segments/finally_the_target_file.py"},
        )],
        model="opus",
    )
    summary = _summarize_with_cwd(msg, "/Users/foo/proj")
    # Original ends with the filename — middle-elide should keep it.
    assert "finally_the_target_file.py)" in summary


def test_summary_assistant_message_keeps_right_truncate_for_bash():
    # Bash commands stay right-truncated — middle-elide would cut out the
    # interesting middle of a long command. Make sure the path-tool branch
    # doesn't accidentally apply to Bash.
    msg = AssistantMessage(
        content=[ToolUseBlock(
            id="toolu_01BASH", name="Bash",
            input={"command": "x" * 200},
        )],
        model="opus",
    )
    summary = _sdk_message_summary(msg)
    # Right-truncate marker at the end, not the middle.
    assert summary.split("(")[1].rstrip(")] model=opus".rstrip()).endswith("…")


def test_summary_assistant_message_tool_input_newlines_collapsed():
    msg = AssistantMessage(
        content=[ToolUseBlock(
            id="toolu_01ABCDEF", name="Bash",
            input={"command": "git commit -m 'line1\nline2'"},
        )],
        model="opus",
    )
    summary = _sdk_message_summary(msg)
    assert "\n" not in summary
    assert "line1 line2" in summary


def test_summary_assistant_message_pure_text_collapses_to_bare_preview():
    # Pure-text assistant replies (no tools, no thinking, no error/stop_reason)
    # render as just the quoted text — the surrounding `text_blocks=1
    # text_len=… text_preview="…" model=…` scaffolding was pure noise around
    # short chat lines.
    msg = AssistantMessage(
        content=[TextBlock("hello world")],
        model="claude-sonnet-4-6",
    )
    assert _sdk_message_summary(msg) == '"hello world"'


def test_summary_assistant_message_surfaces_text_preview():
    # Short intermediate replies aren't echoed in ResultMessage.result (only
    # the FINAL turn output is). Without surfacing the text on the assistant
    # line those mid-turn statements vanish from the log.
    msg = AssistantMessage(
        content=[TextBlock("Agent 1 done, starting agent 2.")],
        model="opus",
    )
    assert _sdk_message_summary(msg) == '"Agent 1 done, starting agent 2."'


def test_summary_assistant_message_text_preview_joins_multiple_blocks():
    msg = AssistantMessage(
        content=[TextBlock("First."), TextBlock("Then second.")],
        model="opus",
    )
    assert _sdk_message_summary(msg) == '"First. Then second."'


def test_summary_assistant_message_surfaces_thinking_length():
    msg = AssistantMessage(
        content=[ThinkingBlock(thinking="reasoning text", signature="sig")],
        model="opus",
    )
    summary = _sdk_message_summary(msg)
    assert "thinking_len=14" in summary


def test_summary_assistant_message_thinking_surfaces_usage_tokens():
    # Thinking blocks can burn substantial output tokens — surface the per-
    # message usage so the cost of an extended-thinking run is visible at INFO
    # without waiting for the terminal ResultMessage.
    msg = AssistantMessage(
        content=[ThinkingBlock(thinking="long reasoning", signature="sig")],
        model="opus",
        usage={
            "input_tokens": 6,
            "output_tokens": 1500,
            "cache_read_input_tokens": 35500,
        },
    )
    summary = _sdk_message_summary(msg)
    assert "input=6" in summary
    assert "output=1.5K" in summary
    assert "cache_read=35.5K" in summary


def test_summary_assistant_message_non_thinking_omits_usage_tokens():
    # Usage fields are only attached to thinking lines — adding them to every
    # tool-call line would bury the actual tool entries in repetitive noise.
    msg = AssistantMessage(
        content=[ToolUseBlock(id="toolu_01abc", name="Bash", input={"command": "ls"})],
        model="opus",
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    summary = _sdk_message_summary(msg)
    assert "input=" not in summary
    assert "cache_read=" not in summary


def test_summary_assistant_message_surfaces_parent_tool_use_id_for_subagent():
    # parent_tool_use_id != None marks a message emitted from inside a
    # subagent invocation — the only signal of that lineage.
    msg = AssistantMessage(
        content=[TextBlock("inner reply")],
        model="opus",
        parent_tool_use_id="toolu_01PARENTxyz",
    )
    summary = _sdk_message_summary(msg)
    assert "parent_tool_use_id=01PARENT" in summary


def test_summary_assistant_message_omits_stop_reason_when_sdk_gives_none():
    # Observation result (verified by monkeypatching parse_message): every
    # AssistantMessage in the SDK stream has `stop_reason=None`. The CLI's
    # wire format omits it; only the JSONL persistence layer fills it in.
    # So showing `stop_reason=-` on every assistant line was pure noise —
    # surface only when actually set (e.g. the rare stop_sequence case).
    msg = AssistantMessage(
        content=[TextBlock("hi")], model="opus", stop_reason=None
    )
    assert "stop_reason" not in _sdk_message_summary(msg)


def test_summary_assistant_message_still_shows_stop_reason_when_set():
    # When the SDK does populate it (observed: stop_sequence cases), keep
    # surfacing it — the value is operationally meaningful.
    msg = AssistantMessage(
        content=[TextBlock("hi")], model="opus", stop_reason="stop_sequence"
    )
    assert "stop_reason=stop_sequence" in _sdk_message_summary(msg)


def test_summary_assistant_message_surfaces_error_kind():
    # SDK populates `error` (e.g. rate_limit/server_error) when a stream errors —
    # surface it so logs aren't silent about why a turn went sideways.
    msg = AssistantMessage(content=[], model="claude-opus-4-7", error="server_error")
    assert "error=server_error" in _sdk_message_summary(msg)


def test_summary_assistant_message_empty_content_only_shows_model():
    # No content blocks → no tools/text/thinking fields; with stop_reason=None
    # filtered out and no error, only `model` remains.
    msg = AssistantMessage(content=[], model="claude-opus-4-7")
    summary = _sdk_message_summary(msg)
    assert summary == "model=claude-opus-4-7"


def test_summary_user_message_tool_results_show_id_and_done_err():
    # Same id-prefix-stripping fix as AssistantMessage. Success result
    # contents are intentionally not surfaced (can be huge file reads); only
    # the done/err flag is kept.
    msg = UserMessage(
        content=[
            ToolResultBlock(tool_use_id="toolu_abc12345xyz", content="ok"),
            ToolResultBlock(tool_use_id="toolu_def67890qrs", content="boom", is_error=True),
        ],
        parent_tool_use_id="toolu_parent999abc",
    )
    assert _sdk_message_summary(msg) == (
        "agent=sub tool_results=[abc12345(done),def67890(err: boom)] "
        "parent_tool_use_id=parent99"
    )


def test_summary_user_message_tool_results_carry_tool_name_when_recorded():
    # When AssistantMessage already announced the tool_use (and thus
    # log_context recorded id→name), the matching tool_result line prefixes
    # the result with the tool name — so you can tell `Bash` finished vs.
    # `Read` finished without scrolling up.
    use = AssistantMessage(
        content=[ToolUseBlock(id="toolu_01abc", name="Bash", input={"command": "ls"})],
        model="opus",
    )
    _ = _sdk_message_summary(use)  # records id→name
    result = UserMessage(
        content=[ToolResultBlock(tool_use_id="toolu_01abc", content="ok")],
    )
    summary = _sdk_message_summary(result)
    assert "tool_results=[Bash#01abc(done" in summary  # name surfaced


def test_summary_user_message_tool_results_include_duration():
    # Wall-clock elapsed from tool_use → tool_result. We can't easily check
    # an exact ms, but the duration field must be present.
    use = AssistantMessage(
        content=[ToolUseBlock(id="toolu_01abc", name="Bash", input={"command": "ls"})],
        model="opus",
    )
    _ = _sdk_message_summary(use)
    result = UserMessage(
        content=[ToolResultBlock(tool_use_id="toolu_01abc", content="ok")],
    )
    summary = _sdk_message_summary(result)
    # Format is `(done 5ms)` or `(done 1.2s)` — anything ending in `ms` or `s`
    # right before the closing paren.
    import re
    assert re.search(r"\(done \d+(ms|\.\d+s)\)", summary), summary


def test_summary_user_message_tool_results_fall_back_to_id_when_unrecorded():
    # No prior AssistantMessage in this turn — degrade gracefully to id-only.
    # This happens at turn boundaries or for subagent results.
    result = UserMessage(
        content=[ToolResultBlock(tool_use_id="toolu_01orphan", content="ok")],
    )
    summary = _sdk_message_summary(result)
    assert "tool_results=[01orphan(done)]" in summary
    assert "Bash" not in summary  # no leaked name


def test_summary_user_message_tool_results_error_includes_name_and_duration():
    use = AssistantMessage(
        content=[ToolUseBlock(id="toolu_01abc", name="Bash", input={"command": "nope"})],
        model="opus",
    )
    _ = _sdk_message_summary(use)
    result = UserMessage(content=[
        ToolResultBlock(tool_use_id="toolu_01abc", content="command not found", is_error=True),
    ])
    summary = _sdk_message_summary(result)
    import re
    # Format: `Bash#01abc(err 5ms: command not found)`
    assert re.search(r"Bash#01abc\(err \d+(ms|\.\d+s): command not found\)", summary), summary


def test_summary_ask_user_question_answered_renders_as_answered_not_err():
    # SDK convention: AskUserQuestion always comes back as PermissionResultDeny
    # so is_error=True. The flag must reflect the real outcome (user answered)
    # rather than blindly say `err` like every other deny.
    use = AssistantMessage(
        content=[ToolUseBlock(
            id="toolu_01ABCDEF", name="AskUserQuestion",
            input={"questions": [{"question": "name?"}]},
        )],
        model="opus",
    )
    _ = _sdk_message_summary(use)
    result = UserMessage(content=[
        ToolResultBlock(
            tool_use_id="toolu_01ABCDEF",
            content="The user answered your AskUserQuestion via DingTalk: ...",
            is_error=True,
        ),
    ])
    summary = _sdk_message_summary(result)
    assert "AskUserQuestion#01ABCDEF(answered" in summary
    assert "AskUserQuestion#01ABCDEF(err" not in summary


def test_ask_question_status_handles_list_content():
    # Newer SDK wire-format delivers PermissionResultDeny.message as a list
    # of {type:"text",text:"…"} parts rather than a bare string. The helper
    # concatenates the text fields before matching the answered/no_answer
    # prefix.
    from claude_dingtalk_bridge.claude_runner import _ask_question_status

    answered = _ask_question_status([
        {"type": "text", "text": "The user answered your AskUserQuestion"},
        {"type": "text", "text": " via DingTalk: - foo: bar"},
    ])
    assert answered == "answered"
    no_answer = _ask_question_status([
        {"type": "text", "text": "The user did not answer your AskUserQuestion"},
    ])
    assert no_answer == "no_answer"
    # None/empty content also falls into the safe "no_answer" branch — the
    # daemon shouldn't crash if the SDK ever delivers an empty list.
    assert _ask_question_status(None) == "no_answer"
    assert _ask_question_status([]) == "no_answer"


def test_summary_ask_user_question_no_answer_renders_as_no_answer():
    # Cancelled/timed-out variant: orchestrator returns a "did not answer"
    # message via PermissionResultDeny — flag should be `no_answer`.
    use = AssistantMessage(
        content=[ToolUseBlock(
            id="toolu_01ABCDEF", name="AskUserQuestion",
            input={"questions": [{"question": "name?"}]},
        )],
        model="opus",
    )
    _ = _sdk_message_summary(use)
    result = UserMessage(content=[
        ToolResultBlock(
            tool_use_id="toolu_01ABCDEF",
            content="The user did not answer your AskUserQuestion via DingTalk "
                    "(cancelled or timed out). Do not retry; proceed with a "
                    "reasonable default.",
            is_error=True,
        ),
    ])
    summary = _sdk_message_summary(result)
    assert "AskUserQuestion#01ABCDEF(no_answer" in summary


def test_summary_user_message_error_with_list_content_extracts_text_parts():
    # When SDK delivers tool errors as a list of {type, text} parts, the
    # preview must concatenate the text fields rather than render the raw
    # Python list (which would be unreadable).
    msg = UserMessage(
        content=[
            ToolResultBlock(
                tool_use_id="toolu_aabbccdd",
                content=[
                    {"type": "text", "text": "command failed"},
                    {"type": "text", "text": "with exit 1"},
                ],
                is_error=True,
            ),
        ],
    )
    summary = _sdk_message_summary(msg)
    assert "aabbccdd(err: command failed with exit 1)" in summary


def test_summary_user_message_error_without_content_just_flags_err():
    msg = UserMessage(
        content=[
            ToolResultBlock(tool_use_id="toolu_aabbccdd", content=None, is_error=True),
        ],
    )
    summary = _sdk_message_summary(msg)
    assert "aabbccdd(err)" in summary
    assert "err:" not in summary  # no colon when there's no preview text


def test_summary_user_message_text_blocks_surface_with_preview():
    # Some UserMessages carry TextBlock content (Skill output, system
    # reminder injections). Previously rendered as a useless bare `-`;
    # now they show length and a preview so the line carries info.
    msg = UserMessage(
        content=[TextBlock(text="Reminder: the user prefers concise answers.\nKeep it short.")],
    )
    summary = _sdk_message_summary(msg)
    assert "text_len=58" in summary
    assert 'text_preview="Reminder: the user prefers concise answers. Keep it short."' in summary


def test_summary_user_message_mixed_tool_results_and_text():
    # When both result + text blocks are present, surface both — neither
    # should shadow the other.
    msg = UserMessage(
        content=[
            ToolResultBlock(tool_use_id="toolu_abc12345", content="ok"),
            TextBlock(text="Extra context follows."),
        ],
    )
    summary = _sdk_message_summary(msg)
    assert "tool_results=[abc12345(done)]" in summary
    assert "text_len=22" in summary


def test_summary_user_message_success_surfaces_skill_content():
    # Skill's tool result is a short banner like
    # ``Launching skill: superpowers:brainstorming`` — genuinely useful on the
    # log without bloating it. Other tools' success content stays hidden (file
    # reads etc. can be huge).
    use = AssistantMessage(
        content=[ToolUseBlock(
            id="toolu_01skill", name="Skill",
            input={"skill": "superpowers:brainstorming"},
        )],
        model="opus",
    )
    _ = _sdk_message_summary(use)  # records id→name
    result = UserMessage(
        content=[ToolResultBlock(
            tool_use_id="toolu_01skill",
            content="Launching skill: superpowers:brainstorming",
        )],
    )
    summary = _sdk_message_summary(result)
    assert "Skill#01skill(done" in summary
    assert "Launching skill: superpowers:brainstorming" in summary


def test_summary_user_message_success_does_not_surface_content_preview():
    # Successful tool results can be huge (full file reads, long Bash output).
    # Surface only the ok flag — full content lives in the JSONL transcript.
    msg = UserMessage(
        content=[
            ToolResultBlock(
                tool_use_id="toolu_aabbccdd",
                content="a" * 1000,
                is_error=False,
            ),
        ],
    )
    summary = _sdk_message_summary(msg)
    assert "aabbccdd(done)" in summary
    assert "a" * 100 not in summary  # no content preview leaked


def test_summary_result_message_includes_duration_and_status():
    msg = ResultMessage(
        subtype="success",
        duration_ms=28235,
        duration_api_ms=27000,
        is_error=False,
        num_turns=4,
        session_id="s1",
        stop_reason="end_turn",
    )
    summary = _sdk_message_summary(msg)
    assert "subtype=success" in summary
    assert "duration_ms=28235" in summary
    # api time separated from total — the diff explains SDK/queue overhead.
    assert "duration_api_ms=27000" in summary
    assert "num_turns=4" in summary
    assert "stop_reason=end_turn" in summary
    # is_error=False is filtered (only surfaced when True).
    assert "is_error" not in summary


def test_summary_result_message_surfaces_result_text_preview():
    # Without this preview the daemon log has no trace of what the model
    # actually concluded; phone-side reply is the only record otherwise.
    msg = ResultMessage(
        subtype="success",
        duration_ms=100, duration_api_ms=80,
        is_error=False, num_turns=1, session_id="s1",
        result="git push succeeded.\nThe feat commit was rejected.",
    )
    summary = _sdk_message_summary(msg)
    assert 'result="git push succeeded. The feat commit was rejected."' in summary


def test_summary_result_message_result_preview_truncated():
    msg = ResultMessage(
        subtype="success",
        duration_ms=100, duration_api_ms=80,
        is_error=False, num_turns=1, session_id="s1",
        result="x" * 500,
    )
    summary = _sdk_message_summary(msg)
    assert "…" in summary
    assert "x" * 200 not in summary


def test_summary_result_message_no_result_text_omits_field():
    msg = ResultMessage(
        subtype="success",
        duration_ms=100, duration_api_ms=80,
        is_error=False, num_turns=1, session_id="s1",
        result=None,
    )
    assert "result=" not in _sdk_message_summary(msg)


def test_summary_result_message_surfaces_error_status():
    msg = ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=80,
        is_error=True,
        num_turns=1,
        session_id="s1",
        api_error_status=429,
    )
    summary = _sdk_message_summary(msg)
    assert "is_error=True" in summary
    assert "api_error_status=429" in summary


def test_summary_result_message_surfaces_cost_denials_and_errors():
    msg = ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=False,
        num_turns=3,
        session_id="s1",
        stop_reason="end_turn",
        total_cost_usd=0.1234,
        permission_denials=["x", "y"],
        errors=["disk full"],
    )
    summary = _sdk_message_summary(msg)
    assert "cost=$0.1234" in summary
    assert "permission_denials=[x,y]" in summary
    assert "errors=1:disk full" in summary


def test_summary_result_message_denials_extract_tool_name_from_dict():
    # SDK types permission_denials as list[Any] with no promised schema; when
    # entries are dicts (CLI's actual wire shape), pull the tool_name field
    # rather than dumping the whole dict.
    msg = ResultMessage(
        subtype="success",
        duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
        session_id="s1",
        permission_denials=[{"tool_name": "Bash", "tool_use_id": "x"},
                            {"tool_name": "WebFetch"}],
    )
    assert "permission_denials=[Bash,WebFetch]" in _sdk_message_summary(msg)


def test_summary_result_message_service_tier_omitted_when_standard():
    # service_tier="standard" is the default — logging it on every turn would
    # be pure noise.
    msg = ResultMessage(
        subtype="success",
        duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
        session_id="s1",
        usage={"service_tier": "standard", "inference_geo": ""},
    )
    summary = _sdk_message_summary(msg)
    assert "service_tier" not in summary
    assert "inference_geo" not in summary


def test_summary_result_message_service_tier_surfaced_when_non_default():
    # A turn that suddenly runs on a different tier or region is the kind of
    # thing that explains a latency or cost anomaly — surface it.
    msg = ResultMessage(
        subtype="success",
        duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
        session_id="s1",
        usage={"service_tier": "batch", "inference_geo": "us-east-1"},
    )
    summary = _sdk_message_summary(msg)
    assert "service_tier=batch" in summary
    assert "inference_geo=us-east-1" in summary


def test_summary_result_message_iterations_silent_when_one():
    # usage.iterations[] has length>1 only when Anthropic's server splits one
    # logical response into multiple internal calls (rare). The common
    # length==1 case would just duplicate outer usage — noise.
    msg = ResultMessage(
        subtype="success",
        duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
        session_id="s1",
        usage={"iterations": [{"input_tokens": 1}]},
    )
    assert "iterations" not in _sdk_message_summary(msg)


def test_summary_result_message_iterations_surfaced_when_multi():
    msg = ResultMessage(
        subtype="success",
        duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
        session_id="s1",
        usage={"iterations": [{}, {}, {}]},
    )
    assert "iterations=3" in _sdk_message_summary(msg)


def test_summary_user_message_tool_error_preview_uses_wider_limit():
    # Error stacks lose diagnostic value when chopped at 80; surface 200 and
    # tag content_len so the operator knows truncation happened and where to
    # look for the rest.
    long_err = "stack trace " * 30  # ~360 chars
    msg = UserMessage(content=[
        ToolResultBlock(tool_use_id="toolu_01abc", content=long_err, is_error=True),
    ])
    summary = _sdk_message_summary(msg)
    assert "content_len=" in summary
    # 200-char window keeps far more than the previous 80.
    assert "stack trace stack trace stack trace stack trace stack trace" in summary


def test_summary_user_message_tool_error_short_no_content_len():
    # Short errors fit within 200 — no content_len signal needed.
    msg = UserMessage(content=[
        ToolResultBlock(tool_use_id="toolu_01abc", content="boom", is_error=True),
    ])
    summary = _sdk_message_summary(msg)
    assert "content_len" not in summary
    assert "(err: boom)" in summary


def test_summary_init_surfaces_cache_ttl_policy():
    # _build_options forces ENABLE_PROMPT_CACHING_1H unconditionally — the
    # init line is the natural spot to confirm the active policy alongside
    # model/cwd/version.
    from claude_agent_sdk import SystemMessage
    msg = SystemMessage(subtype="init", data={"model": "opus", "cwd": "/tmp/proj"})
    assert "cache_ttl_policy=1h" in _sdk_message_summary(msg)


def test_summary_rate_limit_event_surfaces_status_and_utilization():
    msg = RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="allowed_warning",
            rate_limit_type="five_hour",
            utilization=0.87,
            resets_at=1779550000,
        ),
        uuid="u1",
        session_id="s1",
    )
    summary = _sdk_message_summary(msg)
    assert "status=allowed_warning" in summary
    assert "type=five_hour" in summary
    assert "utilization=87.0%" in summary
    assert "resets_at=1779550000 (" in summary  # annotated with local time


def test_summary_task_started_includes_description_truncated():
    msg = TaskStartedMessage(
        subtype="task_started",
        data={"subagent_type": "general-purpose"},
        task_id="task-1",
        description="x" * 80,
        uuid="u1",
        session_id="s1",
    )
    summary = _sdk_message_summary(msg)
    assert "task_id=task-1" in summary
    assert "subagent_type=general-purpose" in summary
    assert "desc=" + "x" * 60 + "…" in summary


def test_summary_task_progress_includes_usage_counters():
    msg = TaskProgressMessage(
        subtype="task_progress",
        data={},
        task_id="task-1",
        description="desc",
        usage={"tool_uses": 3, "total_tokens": 1500, "duration_ms": 5000},
        uuid="u1",
        session_id="s1",
        last_tool_name="Bash",
    )
    summary = _sdk_message_summary(msg)
    assert "task_id=task-1" in summary
    assert "tool_uses=3" in summary
    assert "total_tokens=1500" in summary
    assert "last_tool=Bash" in summary


def test_summary_task_notification_includes_status_and_duration():
    msg = TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id="task-1",
        status="completed",
        output_file="/tmp/out",
        summary="done",
        uuid="u1",
        session_id="s1",
        usage={"tool_uses": 2, "total_tokens": 100, "duration_ms": 12000},
    )
    summary = _sdk_message_summary(msg)
    assert "task_id=task-1" in summary
    assert "status=completed" in summary
    assert "duration_ms=12000" in summary
    # tool_uses + total_tokens carry the subagent's final accounting —
    # task_progress is now DEBUG-only, so this line is the only INFO source.
    assert "tool_uses=2" in summary
    assert "total_tokens=100" in summary


def test_subagent_tag_on_assistant_after_task_started():
    # TaskStartedMessage with subagent_type registers the (tool_use_id →
    # task_id+type) mapping. Subsequent AssistantMessage whose
    # parent_tool_use_id matches that tool_use_id renders agent=sub, sub_id
    # (8-char prefix of task_id), and sub_type — saving the operator from
    # pairing parent_tool_use_id with the task_started line by eye.
    from claude_dingtalk_bridge import log_context
    log_context.clear()
    started = TaskStartedMessage(
        subtype="task_started",
        data={"subagent_type": "Explore"},
        task_id="a3bedf0ef8c8d70cb",
        description="Research thing",
        uuid="u1",
        session_id="s1",
        tool_use_id="toolu_01ENTBidxyz",
    )
    _sdk_message_summary(started)  # registers the subagent
    child = AssistantMessage(
        content=[ToolUseBlock(id="toolu_01AHam12", name="Bash", input={"command": "ls"})],
        model="claude-haiku-4-5",
        parent_tool_use_id="toolu_01ENTBidxyz",
    )
    summary = _sdk_message_summary(child)
    assert summary.startswith("agent=sub sub_id=a3bedf0e sub_type=Explore "), summary
    assert "tools=[Bash#01AHam12(ls)]" in summary
    assert "parent_tool_use_id=01ENTBi" in summary


def test_subagent_tag_on_user_tool_result():
    # Symmetric path: a tool_result message from inside a subagent gets the
    # same agent/sub_id/sub_type lead.
    from claude_dingtalk_bridge import log_context
    log_context.clear()
    started = TaskStartedMessage(
        subtype="task_started",
        data={"subagent_type": "Explore"},
        task_id="a3bedf0ef8c8d70cb",
        description="x",
        uuid="u1",
        session_id="s1",
        tool_use_id="toolu_01ENTBidxyz",
    )
    _sdk_message_summary(started)
    msg = UserMessage(
        content=[ToolResultBlock(tool_use_id="toolu_01abc", content="ok")],
        parent_tool_use_id="toolu_01ENTBidxyz",
    )
    summary = _sdk_message_summary(msg)
    assert summary.startswith("agent=sub sub_id=a3bedf0e sub_type=Explore "), summary


def test_subagent_main_turn_unchanged():
    # Main-turn messages (parent_tool_use_id=None) must not gain any sub
    # fields — log format for the common case stays exactly as before.
    from claude_dingtalk_bridge import log_context
    log_context.clear()
    msg = AssistantMessage(
        content=[ToolUseBlock(id="toolu_01abc", name="Bash", input={"command": "ls"})],
        model="opus",
    )
    summary = _sdk_message_summary(msg)
    assert "agent=" not in summary
    assert "sub_id=" not in summary
    assert "sub_type=" not in summary


def test_subagent_parallel_subagents_tagged_independently():
    # Multiple subagents in flight at once (observed: Explore agents dispatched
    # in parallel). Each child message looks up by its own parent_tool_use_id
    # and gets that subagent's task_id+type, not whichever was registered last.
    from claude_dingtalk_bridge import log_context
    log_context.clear()
    a = TaskStartedMessage(
        subtype="task_started", data={"subagent_type": "Explore"},
        task_id="aaaaaaaa11111111", description="A",
        uuid="u1", session_id="s1", tool_use_id="toolu_PARENT_A",
    )
    b = TaskStartedMessage(
        subtype="task_started", data={"subagent_type": "code-reviewer"},
        task_id="bbbbbbbb22222222", description="B",
        uuid="u2", session_id="s1", tool_use_id="toolu_PARENT_B",
    )
    _sdk_message_summary(a)
    _sdk_message_summary(b)
    child_a = AssistantMessage(
        content=[TextBlock("from A")], model="haiku",
        parent_tool_use_id="toolu_PARENT_A",
    )
    child_b = AssistantMessage(
        content=[TextBlock("from B")], model="haiku",
        parent_tool_use_id="toolu_PARENT_B",
    )
    sa = _sdk_message_summary(child_a)
    sb = _sdk_message_summary(child_b)
    assert "sub_id=aaaaaaaa sub_type=Explore" in sa
    assert "sub_id=bbbbbbbb sub_type=code-reviewer" in sb


def test_subagent_orphan_parent_falls_back_to_agent_sub_alone():
    # parent_tool_use_id is set but the map has no matching entry — e.g.
    # daemon restart mid-subagent, or task_started arrived in a prior turn
    # whose log_context was cleared. The line still must signal "from a
    # subagent" — agent=sub fires purely from parent_tool_use_id, sub_id /
    # sub_type silently omitted.
    from claude_dingtalk_bridge import log_context
    log_context.clear()
    msg = AssistantMessage(
        content=[TextBlock("orphan child")], model="haiku",
        parent_tool_use_id="toolu_UNKNOWN_PARENT",
    )
    summary = _sdk_message_summary(msg)
    assert summary.startswith("agent=sub "), summary
    assert "sub_id=" not in summary
    assert "sub_type=" not in summary


def test_subagent_task_notification_clears_map():
    # TaskNotificationMessage forgets the entry, so post-subagent lookups
    # (e.g. a stray tool_result that arrives after the notification) fall
    # through to the orphan path rather than picking up stale tags.
    from claude_dingtalk_bridge import log_context
    log_context.clear()
    started = TaskStartedMessage(
        subtype="task_started", data={"subagent_type": "Explore"},
        task_id="a3bedf0e11112222", description="x",
        uuid="u1", session_id="s1", tool_use_id="toolu_01ENTBid",
    )
    _sdk_message_summary(started)
    ended = TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="a3bedf0e11112222",
        status="completed", output_file="/tmp/o", summary="done",
        uuid="u2", session_id="s1", tool_use_id="toolu_01ENTBid",
    )
    _sdk_message_summary(ended)
    sub_id, sub_type = log_context.lookup_subagent("toolu_01ENTBid")
    assert sub_id is None and sub_type is None


def test_subagent_skipped_when_no_subagent_type():
    # Background Bash (run_in_background=true) also emits task_started but
    # without subagent_type — it is NOT a subagent and has no child
    # AssistantMessages to tag. The map must not record it; otherwise some
    # unrelated future message could accidentally inherit a stale tag.
    from claude_dingtalk_bridge import log_context
    log_context.clear()
    started = TaskStartedMessage(
        subtype="task_started", data={},  # no subagent_type
        task_id="b0gh0r1zt", description="Find daemon log file paths",
        uuid="u1", session_id="s1", tool_use_id="toolu_BG_BASH",
    )
    _sdk_message_summary(started)
    sub_id, sub_type = log_context.lookup_subagent("toolu_BG_BASH")
    assert sub_id is None and sub_type is None


def test_summary_mirror_error_truncates_long_error():
    msg = MirrorErrorMessage(
        subtype="mirror_error",
        data={},
        error="boom " * 30,
    )
    summary = _sdk_message_summary(msg)
    assert summary.startswith("error=")
    assert summary.endswith("…")


def test_summary_hook_event_includes_hook_name_and_tool():
    msg = HookEventMessage(
        subtype="hook_started",
        data={"tool_name": "Bash"},
        hook_event_name="PreToolUse",
    )
    summary = _sdk_message_summary(msg)
    assert "subtype=hook_started" in summary
    assert "hook=PreToolUse" in summary
    assert "tool=Bash" in summary


def test_summary_system_message_init_shows_model_and_cwd():
    msg = SystemMessage(
        subtype="init",
        data={"model": "claude-opus-4-7", "cwd": "/tmp/proj"},
    )
    summary = _sdk_message_summary(msg)
    assert "subtype=init" in summary
    assert "model=claude-opus-4-7" in summary
    assert "cwd=/tmp/proj" in summary


def test_summary_system_message_init_surfaces_permission_mode_and_version():
    # Plan/auto/edit modes alter tool behavior; the version helps tie a
    # production bug to a specific CLI build.
    msg = SystemMessage(
        subtype="init",
        data={
            "model": "claude-opus-4-7",
            "cwd": "/tmp",
            "permissionMode": "auto",
            "claude_code_version": "2.1.146",
        },
    )
    summary = _sdk_message_summary(msg)
    assert "permission_mode=auto" in summary
    assert "version=2.1.146" in summary


def test_summary_task_updated_surfaces_task_id_and_status():
    # Before this branch, task_updated fell through generic SystemMessage and
    # rendered as a bare empty line (`task_updated` with nothing after).
    msg = SystemMessage(
        subtype="task_updated",
        data={"task_id": "a5089e7feb9e2d86f", "patch": {"status": "completed"}},
    )
    summary = _sdk_message_summary(msg)
    assert "task_id=a5089e7feb9e2d86f" in summary
    assert "status=completed" in summary


def test_summary_task_updated_handles_missing_patch():
    # Be tolerant of malformed shapes — at minimum surface what we know.
    msg = SystemMessage(
        subtype="task_updated",
        data={"task_id": "x"},
    )
    summary = _sdk_message_summary(msg)
    assert "task_id=x" in summary


def test_summary_init_collapses_home_in_cwd():
    # Init's cwd is almost always inside $HOME — render it with `~/` so the
    # column stays readable instead of repeating /Users/<long-name>/… on every
    # session start.
    import os
    home = os.path.expanduser("~")
    msg = SystemMessage(
        subtype="init",
        data={"model": "opus", "cwd": f"{home}/Projects/foo"},
    )
    summary = _sdk_message_summary(msg)
    assert "cwd=~/Projects/foo" in summary
    assert home not in summary


def test_summary_init_leaves_external_cwd_alone():
    # cwd outside HOME must stay absolute — don't silently rewrite /tmp/proj.
    msg = SystemMessage(
        subtype="init",
        data={"model": "opus", "cwd": "/tmp/proj"},
    )
    assert "cwd=/tmp/proj" in _sdk_message_summary(msg)


def test_summary_init_omits_session_id():
    # Reverted: session_id field was always redundant with the leading
    # `session=…` column (because _consume calls _note_system_message before
    # _log_sdk_message, the column already shows init's session_id by render
    # time). Multi-init scenarios are distinguished by the session COLUMN
    # changing, not a separate field.
    msg = SystemMessage(
        subtype="init",
        data={
            "model": "opus", "cwd": "/tmp",
            "session_id": "9321bb41-foo-bar",
        },
    )
    summary = _sdk_message_summary(msg)
    assert "session_id" not in summary


def test_summary_compact_boundary_surfaces_pre_tokens_and_trigger():
    msg = SystemMessage(
        subtype="compact_boundary",
        data={"compact_metadata": {"pre_tokens": 45000, "trigger": "manual"}},
    )
    summary = _sdk_message_summary(msg)
    assert "pre_tokens=45000" in summary
    assert "trigger=manual" in summary


def test_summary_permission_denied_dumps_data_keys():
    # We don't yet have a confirmed CLI shape for this message (couldn't
    # reproduce in test). The catch-all dump surfaces whatever fields appear,
    # so the next production hit tells us what's actually there.
    msg = SystemMessage(
        subtype="permission_denied",
        data={
            "subtype": "permission_denied",  # excluded (already in verb)
            "type": "system",  # excluded
            "uuid": "u1",  # excluded
            "session_id": "s1",  # excluded
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
            "reason": "Denied by user",
        },
    )
    summary = _sdk_message_summary(msg)
    assert "tool_name=Bash" in summary
    assert "rm -rf /" in summary
    assert "reason=Denied by user" in summary
    # Excluded envelope fields don't leak through.
    assert "uuid=" not in summary
    assert "session_id=" not in summary


def test_summary_hook_response_surfaces_exit_code_and_outcome():
    msg = HookEventMessage(
        subtype="hook_response",
        data={"exit_code": 1, "outcome": "blocked", "stderr": "permission denied\n"},
        hook_event_name="PreToolUse",
    )
    summary = _sdk_message_summary(msg)
    assert "exit_code=1" in summary
    assert "outcome=blocked" in summary
    assert "stderr=permission denied" in summary


def test_summary_hook_response_skips_empty_stderr():
    # A successful hook (exit 0, no stderr) shouldn't fabricate a stderr field.
    msg = HookEventMessage(
        subtype="hook_response",
        data={"exit_code": 0, "outcome": "success", "stderr": ""},
        hook_event_name="SessionStart",
    )
    summary = _sdk_message_summary(msg)
    assert "stderr" not in summary
    assert "outcome=success" in summary


def test_summary_stream_event_returns_none_to_skip_info():
    # Per-token noise must not flood INFO; DEBUG path still keeps the repr.
    msg = StreamEvent(uuid="u1", session_id="s1", event={"type": "content_block_delta"})
    assert _sdk_message_summary(msg) is None


def test_log_sdk_message_uses_verb_first_naming_for_assistant(caplog):
    # Verb-first: AssistantMessage renders as `assistant tools=…` rather
    # than `sdk_message AssistantMessage tools=…`. The SDK type name lives
    # in the DEBUG dump only.
    msg = AssistantMessage(content=[TextBlock("hi")], model="opus")
    with caplog.at_level(_logging.INFO, logger="claude_dingtalk_bridge.claude_runner"):
        _log_sdk_message(msg)
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == _logging.INFO]
    assert any(l.startswith("assistant ") for l in info_lines)
    assert not any(l.startswith("sdk_message ") for l in info_lines)


def test_log_sdk_message_thinking_only_assistant_gets_thinking_verb(caplog):
    # Extended-thinking turns deliver an AssistantMessage whose content is
    # solely ThinkingBlock(s) — the thinking phase closing snapshot. Promote
    # it to verb `thinking` so it doesn't read as a stray plain assistant row.
    msg = AssistantMessage(
        content=[ThinkingBlock(thinking="", signature="sig")],
        model="claude-opus-4-7",
        usage={"input_tokens": 6, "output_tokens": 8, "cache_read_input_tokens": 0},
    )
    with caplog.at_level(_logging.INFO, logger="claude_dingtalk_bridge.claude_runner"):
        _log_sdk_message(msg)
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == _logging.INFO]
    assert any(l.startswith("thinking ") for l in info_lines)
    assert not any(l.startswith("assistant ") for l in info_lines)


def test_log_sdk_message_assistant_with_text_and_thinking_stays_assistant(caplog):
    # A message that mixes thinking with text or tool_use is still an
    # assistant-level event — the `thinking` verb only kicks in when the
    # snapshot is purely a thinking block.
    msg = AssistantMessage(
        content=[
            ThinkingBlock(thinking="planning…", signature="sig"),
            TextBlock(text="hi"),
        ],
        model="opus",
    )
    with caplog.at_level(_logging.INFO, logger="claude_dingtalk_bridge.claude_runner"):
        _log_sdk_message(msg)
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == _logging.INFO]
    assert any(l.startswith("assistant ") for l in info_lines)
    assert not any(l.startswith("thinking ") for l in info_lines)


def test_log_sdk_message_verb_first_for_known_system_subtypes(caplog):
    # init/permission_denied/compact_boundary/task_updated are promoted to
    # top-level verbs and the redundant `subtype=` prefix is stripped from
    # the summary so the verb isn't repeated.
    msg = SystemMessage(
        subtype="init",
        data={"model": "opus", "cwd": "/tmp"},
    )
    with caplog.at_level(_logging.INFO, logger="claude_dingtalk_bridge.claude_runner"):
        _log_sdk_message(msg)
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == _logging.INFO]
    assert any(l.startswith("init ") for l in info_lines)
    # Redundant `subtype=init` field stripped — the verb already says it.
    assert not any("subtype=init" in l for l in info_lines)


def test_log_sdk_message_demotes_quiet_rate_limit_to_debug(caplog):
    # The steady-state allowed/rejected-overage line shows up every turn —
    # demote to DEBUG so it doesn't dominate the log. Full repr still at DEBUG.
    msg = RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="allowed",
            rate_limit_type="five_hour",
            utilization=None,
            resets_at=1779550000,
            overage_status="rejected",
        ),
        uuid="u", session_id="s",
    )
    with caplog.at_level(_logging.DEBUG, logger="claude_dingtalk_bridge.claude_runner"):
        _log_sdk_message(msg)
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == _logging.INFO]
    assert not info_lines  # nothing at INFO
    debug_lines = [r.getMessage() for r in caplog.records if r.levelno == _logging.DEBUG]
    # Verb matches SDK wire-format name `rate_limit_event`.
    assert any(l.startswith("rate_limit_event ") for l in debug_lines)


def test_log_sdk_message_demotes_hook_events_to_debug(caplog):
    # Hook events carry no operationally useful fields — exit_code/outcome are
    # the diagnostic gold but only when a hook actually fails, and even then
    # the failure surfaces via the tool's permission denial. At INFO they were
    # pure noise around every turn; demote to DEBUG, full repr still preserved.
    msg = HookEventMessage(
        subtype="hook_response",
        data={"exit_code": 0, "outcome": "success"},
        hook_event_name="SessionStart",
    )
    with caplog.at_level(_logging.DEBUG, logger="claude_dingtalk_bridge.claude_runner"):
        _log_sdk_message(msg)
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == _logging.INFO]
    assert not info_lines
    debug_lines = [r.getMessage() for r in caplog.records if r.levelno == _logging.DEBUG]
    assert any(l.startswith("hook_response ") for l in debug_lines)


def test_log_sdk_message_demotes_task_progress_to_debug(caplog):
    # task_progress fires per subagent tool call; same info follows on the
    # next assistant line. Demote to DEBUG to cut noise; counters land at
    # INFO in the terminal task_notification anyway.
    msg = TaskProgressMessage(
        subtype="task_progress",
        data={"subagent_type": "general-purpose"},
        task_id="t1",
        description="d",
        usage={"tool_uses": 1, "total_tokens": 100},
        uuid="u", session_id="s",
        last_tool_name="Read",
    )
    with caplog.at_level(_logging.DEBUG, logger="claude_dingtalk_bridge.claude_runner"):
        _log_sdk_message(msg)
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == _logging.INFO]
    assert not info_lines
    debug_lines = [r.getMessage() for r in caplog.records if r.levelno == _logging.DEBUG]
    assert any(l.startswith("task_progress ") for l in debug_lines)


def test_log_sdk_message_keeps_abnormal_rate_limit_at_info(caplog):
    # A warning or rejected status is exactly when the operator needs to see
    # it — must stay at INFO.
    msg = RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="allowed_warning",
            rate_limit_type="five_hour",
            utilization=0.87,
            resets_at=1779550000,
            overage_status="rejected",
        ),
        uuid="u", session_id="s",
    )
    with caplog.at_level(_logging.INFO, logger="claude_dingtalk_bridge.claude_runner"):
        _log_sdk_message(msg)
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == _logging.INFO]
    assert any("status=allowed_warning" in l for l in info_lines)


def test_log_sdk_message_skips_info_when_summary_is_none(caplog):
    msg = StreamEvent(uuid="u1", session_id="s1", event={"type": "content_block_delta"})
    with caplog.at_level(_logging.DEBUG, logger="claude_dingtalk_bridge.claude_runner"):
        _log_sdk_message(msg)
    assert not [r for r in caplog.records if r.levelno == _logging.INFO]
    debug = [r for r in caplog.records if r.levelno == _logging.DEBUG]
    assert len(debug) == 1
    assert "sdk_message_full StreamEvent" in debug[0].getMessage()


def test_log_sdk_message_emits_info_summary_and_debug_full(caplog):
    msg = TaskStartedMessage(
        subtype="task_started",
        data={"subagent_type": "general-purpose"},
        task_id="task-1",
        description="Fix Task 1",
        uuid="u1",
        session_id="s1",
        task_type="local_agent",
    )
    with caplog.at_level(_logging.DEBUG, logger="claude_dingtalk_bridge.claude_runner"):
        _log_sdk_message(msg)

    info = [r for r in caplog.records if r.levelno == _logging.INFO]
    assert len(info) == 1
    info_text = info[0].getMessage()
    # Verb-first: TaskStartedMessage renders as `task_started …` rather than
    # `sdk_message TaskStartedMessage …`.
    assert info_text.startswith("task_started ")
    assert "task_id=task-1" in info_text
    assert "subagent_type=general-purpose" in info_text
    # Identifier-only summary — descriptions are kept at DEBUG.
    assert "Fix Task 1" in info_text  # description is identifier-ish here, fits.

    debug = [r for r in caplog.records if r.levelno == _logging.DEBUG]
    assert len(debug) == 1
    debug_text = debug[0].getMessage()
    assert "sdk_message_full TaskStartedMessage" in debug_text
    assert "Fix Task 1" in debug_text
    assert "local_agent" in debug_text


# --- run_turn / background drain ---------------------------------------

import asyncio  # noqa: E402

import claude_dingtalk_bridge.claude_runner as cr_mod  # noqa: E402


def _result_message(session_id="sess-1"):
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        result="ok",
    )


class FakeSDKClient:
    """Stand-in for ClaudeSDKClient: replays a scripted message stream.

    Messages tagged after the turn's ResultMessage are only reachable once
    run_turn enters the background-drain phase. A `hang` sentinel makes the
    stream block so the drain timeout can be exercised.
    """

    HANG = object()

    def __init__(self, options=None):
        self.script: list = list(getattr(FakeSDKClient, "next_script", []))
        self.disconnected = False

    async def connect(self):
        pass

    async def query(self, prompt):
        pass

    async def receive_messages(self):
        for msg in self.script:
            if msg is FakeSDKClient.HANG:
                await asyncio.sleep(3600)
            yield msg

    async def disconnect(self):
        self.disconnected = True


async def _run_turn_with(monkeypatch, script, timeout=None, settle=None):
    monkeypatch.setattr(cr_mod, "ClaudeSDKClient", FakeSDKClient)
    if timeout is not None:
        monkeypatch.setattr(cr_mod, "_STUCK_TIMEOUT", timeout)
    if settle is not None:
        monkeypatch.setattr(cr_mod, "_SETTLE_TIMEOUT", settle)
    FakeSDKClient.next_script = script
    runner = ClaudeRunner()
    events = []

    async def emit(event):
        events.append(event)

    await runner.run_turn("/tmp/p", "go", emit)
    return runner, events


async def test_consume_result_message_records_cost_from_total_cost_usd():
    # Wires ResultMessage.total_cost_usd into record_usage so /status's
    # cost line reflects the SDK's authoritative billed amount, not 0.
    from claude_agent_sdk import ResultMessage

    runner = ClaudeRunner()
    msg = ResultMessage(
        subtype="success",
        duration_ms=100, duration_api_ms=80,
        is_error=False, num_turns=1, session_id="s1",
        usage={"input_tokens": 10, "output_tokens": 5},
        total_cost_usd=0.75,
    )

    async def emit(event):
        pass

    await runner._consume(msg, "/tmp/p", emit, set(), {}, set())
    assert runner.session_cost("/tmp/p") == 0.75
    assert runner.last_turn_cost("/tmp/p") == 0.75


async def test_consume_updates_log_context_session_before_logging_init(monkeypatch):
    # Bug fix: previously the init log line itself rendered with `session=-`
    # because _log_sdk_message ran before _note_system_message (which is what
    # populates the contextvar from init.data.session_id). Reversing the
    # order means the init line is the FIRST one tagged with the right
    # session_id, not the second.
    import contextvars
    from claude_agent_sdk import SystemMessage
    from claude_dingtalk_bridge import log_context

    async def go():
        runner = ClaudeRunner()
        init = SystemMessage(subtype="init", data={
            "model": "opus", "cwd": "/tmp",
            "session_id": "9321bb41-init-id",
        })
        observed: list[str] = []

        async def emit(event):
            pass

        # Inject a fake _log_sdk_message that records the session label
        # contextvar AT THE TIME OF LOGGING — that's the value the formatter
        # would prepend.
        def fake_log(message):
            observed.append(log_context.session_label())

        monkeypatch.setattr(cr_mod, "_log_sdk_message", fake_log)
        await runner._consume(init, "/tmp/p", emit, set(), {}, set())
        return observed

    observed = await asyncio.create_task(go())
    assert observed == ["9321bb41"]  # not "-"


async def test_run_turn_stops_at_result_when_no_background(monkeypatch):
    from claude_agent_sdk import TaskNotificationMessage

    from claude_dingtalk_bridge.claude_runner import ResultEvent

    # A notification scripted after the ResultMessage must NOT be consumed:
    # with nothing pending, run_turn ends at the ResultMessage.
    leftover = TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="t9",
        status="completed", output_file="/tmp/o", summary="late",
        uuid="u", session_id="s",
    )
    runner, events = await _run_turn_with(
        monkeypatch, [_result_message(), leftover]
    )
    assert isinstance(events[-1], ResultEvent)
    assert not any(
        getattr(e, "summary", None) == "late" for e in events
    )


async def test_run_turn_drains_background_notification(monkeypatch):
    from claude_agent_sdk import TaskNotificationMessage, TaskStartedMessage

    from claude_dingtalk_bridge.claude_runner import TaskEvent

    started = TaskStartedMessage(
        subtype="task_started", data={"subagent_type": "general-purpose"},
        task_id="t1", description="Solve B", uuid="u", session_id="s",
    )
    notified = TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="t1",
        status="completed", output_file="/tmp/o", summary="B solved",
        uuid="u", session_id="s",
    )
    # started + result end the turn with t1 pending. The drain must reach the
    # notification AND the re-invocation turn that relays the agent's answer —
    # stopping at the notification would cut the answer text off.
    from claude_agent_sdk import AssistantMessage, TextBlock

    from claude_dingtalk_bridge.claude_runner import TextEvent

    relay = AssistantMessage(content=[TextBlock("Agent B answer")], model="opus")
    runner, events = await _run_turn_with(
        monkeypatch,
        [started, _result_message(), notified, relay, _result_message()],
    )
    phases = [e.phase for e in events if isinstance(e, TaskEvent)]
    assert phases == ["started", "notification"]
    assert any(
        isinstance(e, TextEvent) and e.text == "Agent B answer" for e in events
    )


async def test_run_turn_drain_times_out_without_hanging(monkeypatch):
    from claude_agent_sdk import TaskStartedMessage

    from claude_dingtalk_bridge.claude_runner import TaskEvent

    started = TaskStartedMessage(
        subtype="task_started", data={"subagent_type": "general-purpose"},
        task_id="t1", description="Solve B", uuid="u", session_id="s",
    )
    # Notification never arrives — the stream hangs. The drain timeout must
    # release run_turn rather than block forever.
    runner, events = await _run_turn_with(
        monkeypatch,
        [started, _result_message(), FakeSDKClient.HANG],
        timeout=0.05,
    )
    assert runner._active_client is None
    # The unaccounted task surfaces as a timeout event, not silence.
    assert any(
        isinstance(e, TaskEvent) and e.phase == "timeout" for e in events
    )


async def test_cancel_drain_releases_runner_before_timeout(monkeypatch):
    # A fresh prompt arriving mid-drain calls cancel_drain(); the drain must
    # exit cleanly (no timeout TaskEvent) without waiting for the full
    # _STUCK_TIMEOUT.
    from claude_agent_sdk import TaskStartedMessage

    from claude_dingtalk_bridge.claude_runner import TaskEvent

    started = TaskStartedMessage(
        subtype="task_started", data={"subagent_type": "general-purpose"},
        task_id="t1", description="Solve B", uuid="u", session_id="s",
    )
    monkeypatch.setattr(cr_mod, "ClaudeSDKClient", FakeSDKClient)
    # Big timeout: the test must NOT rely on it firing.
    monkeypatch.setattr(cr_mod, "_STUCK_TIMEOUT", 60.0)
    FakeSDKClient.next_script = [started, _result_message(), FakeSDKClient.HANG]
    runner = ClaudeRunner()
    events = []

    async def emit(event):
        events.append(event)

    async def cancel_soon():
        # Yield a few ticks so run_turn reaches drain, then cancel.
        for _ in range(20):
            await asyncio.sleep(0)
            if runner.is_draining:
                break
        assert runner.is_draining, "runner never entered drain phase"
        runner.cancel_drain()

    cancel_task = asyncio.create_task(cancel_soon())
    await asyncio.wait_for(runner.run_turn("/tmp/p", "go", emit), timeout=2.0)
    await cancel_task
    assert runner.is_draining is False
    # No timeout event — the user moved on, so we don't fabricate one.
    assert not any(
        isinstance(e, TaskEvent) and e.phase == "timeout" for e in events
    )


# --- interrupt / can_use_tool ------------------------------------------

async def test_interrupt_forwards_to_active_client():
    runner = ClaudeRunner()
    interrupted = []

    class _Client:
        async def interrupt(self):
            interrupted.append(True)

    runner._active_client = _Client()
    await runner.interrupt()
    assert interrupted == [True]


async def test_interrupt_is_a_noop_without_active_client():
    runner = ClaudeRunner()
    await runner.interrupt()  # no active client — must not raise


async def test_can_use_tool_restamps_session_from_cache():
    # The SDK forks its callback task at connect() time, which means the
    # callback inherits whatever log_context.session was set BEFORE init —
    # for turn 1 of a new project, that's `-`. _can_use_tool reads the
    # cached session id (populated by _note_system_message on init) and
    # restamps the contextvar so the handler's log lines carry the real id.
    from claude_agent_sdk import PermissionResultAllow
    from claude_dingtalk_bridge import log_context

    runner = ClaudeRunner()
    runner._session_ids["/tmp/proj"] = "03cdbc4a-cached-id"

    observed_session: list[str] = []

    async def permission_handler(tool_name, input_data, project_path):
        observed_session.append(log_context._session.get())
        return True

    runner.permission_handler = permission_handler
    options = runner._build_options("/tmp/proj")
    # Start the contextvar at the "stale" sentinel that mirrors orchestrator
    # behaviour before the cache fix.
    log_context.set_session(None)  # → "-"
    assert log_context._session.get() == "-"
    result = await options.can_use_tool("Bash", {"command": "ls"}, None)
    assert isinstance(result, PermissionResultAllow)
    # Inside the handler, the contextvar should be the cached prefix.
    assert observed_session == ["03cdbc4a"]


async def test_can_use_tool_routes_ask_user_question_to_handler():
    from claude_agent_sdk import PermissionResultDeny

    runner = ClaudeRunner()

    async def question_handler(input_data, project_path):
        assert project_path == "/tmp/proj"
        return "the user's answer"

    runner.question_handler = question_handler
    options = runner._build_options("/tmp/proj")
    result = await options.can_use_tool("AskUserQuestion", {"questions": []}, None)
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "the user's answer"
    assert result.interrupt is False


async def test_can_use_tool_allows_when_permission_handler_approves():
    from claude_agent_sdk import PermissionResultAllow

    runner = ClaudeRunner()

    async def permission_handler(tool_name, input_data, project_path):
        return True

    runner.permission_handler = permission_handler
    options = runner._build_options("/tmp/proj")
    result = await options.can_use_tool("Bash", {"command": "ls"}, None)
    assert isinstance(result, PermissionResultAllow)


async def test_can_use_tool_denies_when_permission_handler_rejects():
    from claude_agent_sdk import PermissionResultDeny

    runner = ClaudeRunner()

    async def permission_handler(tool_name, input_data, project_path):
        return False

    runner.permission_handler = permission_handler
    options = runner._build_options("/tmp/proj")
    result = await options.can_use_tool("Bash", {"command": "rm -rf /"}, None)
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "Denied by user via DingTalk"
    assert result.interrupt is False


async def test_can_use_tool_falls_through_to_handler_on_ask():
    # Tool not covered by any rule -> phone (permission_handler) decides.
    from claude_agent_sdk import PermissionResultAllow

    runner = ClaudeRunner()
    runner.permission_rules = PermissionRules(deny=[])
    called = []

    async def permission_handler(tool_name, input_data, project_path):
        called.append(tool_name)
        return True

    runner.permission_handler = permission_handler
    options = runner._build_options("/tmp/proj")
    result = await options.can_use_tool(
        "Bash", {"command": "make test"}, None
    )
    assert isinstance(result, PermissionResultAllow)
    assert called == ["Bash"]  # handler invoked because no rule matched


# --- branch-coverage backfill: untested false-side branches -------------


def test_track_pending_ignores_non_lifecycle_phase():
    # Only started/notification touch the pending set — a progress event
    # must leave it untouched.
    from claude_dingtalk_bridge.claude_runner import TaskEvent, _track_pending

    pending = {"t1"}
    _track_pending(TaskEvent("progress", "t1"), pending)
    assert pending == {"t1"}


def test_translate_skips_blank_text_block():
    # A whitespace-only TextBlock is neither surfaced as text nor a tool call.
    from claude_agent_sdk import AssistantMessage, TextBlock

    from claude_dingtalk_bridge.claude_runner import _translate

    msg = AssistantMessage(content=[TextBlock("   ")], model="opus")
    assert _translate(msg) == []


def test_translate_returns_empty_for_unrecognized_message():
    # A SystemMessage that is not compact_boundary carries no phone-facing text.
    from claude_agent_sdk import SystemMessage

    from claude_dingtalk_bridge.claude_runner import _translate

    assert _translate(SystemMessage(subtype="init", data={})) == []


def test_note_system_message_init_without_model_keeps_none():
    # An init message missing the model field leaves observed_model unset.
    from claude_agent_sdk import SystemMessage

    runner = ClaudeRunner()
    runner._note_system_message(SystemMessage(subtype="init", data={}), "/tmp/proj")
    assert runner.observed_model is None


async def test_run_turn_handles_result_without_session_id(monkeypatch):
    # A ResultMessage with no session_id must not register a session.
    runner, _ = await _run_turn_with(
        monkeypatch, [_result_message(session_id="")]
    )
    assert runner.current_session("/tmp/p") is None


async def test_run_turn_handles_stream_ending_without_result(monkeypatch):
    # The message stream may end without a ResultMessage — the turn loop
    # completes by exhaustion rather than a break, and still emits its events.
    from claude_agent_sdk import AssistantMessage, TextBlock

    from claude_dingtalk_bridge.claude_runner import TextEvent

    runner, events = await _run_turn_with(
        monkeypatch,
        [AssistantMessage(content=[TextBlock("partial")], model="opus")],
    )
    assert any(
        isinstance(e, TextEvent) and e.text == "partial" for e in events
    )


# --- task_updated → acknowledged ----------------------------------------


def test_translate_task_updated_completed_marks_acknowledged():
    # task_updated to a terminal status is the SDK's authoritative "task done"
    # signal — it fires for every subagent, even ones whose answer is inlined
    # into the parent's turn so no TaskNotificationMessage follows. Recording
    # it lets drain short-circuit once every pending task is acknowledged.
    from claude_agent_sdk import SystemMessage

    from claude_dingtalk_bridge.claude_runner import TaskEvent, _translate

    msg = SystemMessage(
        subtype="task_updated",
        data={
            "task_id": "t1",
            "patch": {"status": "completed", "end_time": 1779530508259},
        },
    )
    acknowledged: set[str] = set()
    events = _translate(msg, {}, acknowledged)
    assert acknowledged == {"t1"}
    assert len(events) == 1
    assert isinstance(events[0], TaskEvent)
    assert events[0].phase == "acknowledged"
    assert events[0].task_id == "t1"
    assert events[0].status == "completed"


def test_translate_task_updated_failed_and_stopped_also_acknowledged():
    from claude_agent_sdk import SystemMessage

    from claude_dingtalk_bridge.claude_runner import _translate

    for status in ("failed", "stopped"):
        msg = SystemMessage(
            subtype="task_updated",
            data={"task_id": "t1", "patch": {"status": status}},
        )
        ack: set[str] = set()
        events = _translate(msg, {}, ack)
        assert ack == {"t1"}, f"status={status} did not acknowledge"
        assert events and events[0].phase == "acknowledged"


def test_translate_task_updated_non_terminal_status_ignored():
    # task_updated also fires for transient state changes (e.g. in_progress);
    # only terminal statuses count as acknowledgement.
    from claude_agent_sdk import SystemMessage

    from claude_dingtalk_bridge.claude_runner import _translate

    msg = SystemMessage(
        subtype="task_updated",
        data={"task_id": "t1", "patch": {"status": "in_progress"}},
    )
    ack: set[str] = set()
    assert _translate(msg, {}, ack) == []
    assert ack == set()


def test_translate_task_updated_without_task_id_is_noop():
    # Defensive: a malformed task_updated must not crash translation.
    from claude_agent_sdk import SystemMessage

    from claude_dingtalk_bridge.claude_runner import _translate

    ack: set[str] = set()
    msg = SystemMessage(
        subtype="task_updated", data={"patch": {"status": "completed"}}
    )
    assert _translate(msg, {}, ack) == []
    assert ack == set()


async def test_drain_settle_exit_when_all_pending_acknowledged(monkeypatch):
    # All pending tasks have been task_updated to terminal — no truly-stuck
    # subagent is left, so drain should fall through the settle window and
    # exit silently instead of waiting the full _STUCK_TIMEOUT.
    from claude_agent_sdk import SystemMessage, TaskStartedMessage

    from claude_dingtalk_bridge.claude_runner import TaskEvent

    started = TaskStartedMessage(
        subtype="task_started", data={"subagent_type": "general-purpose"},
        task_id="t1", description="Solve B", uuid="u", session_id="s",
    )
    ack_update = SystemMessage(
        subtype="task_updated",
        data={"task_id": "t1", "patch": {"status": "completed"}},
    )
    # Hard timeout is long; settle is short. Only the settle path can release
    # run_turn within the test's wait_for budget.
    runner, events = await _run_turn_with(
        monkeypatch,
        [started, ack_update, _result_message(), FakeSDKClient.HANG],
        timeout=30.0,
        settle=0.05,
    )
    assert runner._active_client is None
    # The acknowledged task must NOT surface as a timeout — SDK already told
    # us it finished, we just never got a relay turn.
    assert not any(
        isinstance(e, TaskEvent) and e.phase == "timeout" for e in events
    )


async def test_drain_stuck_timeout_emits_only_for_unacknowledged(monkeypatch):
    # Two subagents pending at ResultMessage: one acknowledged via
    # task_updated, one truly stuck. The hard timeout must fire a `timeout`
    # event for the stuck one only — the acknowledged one is silent.
    from claude_agent_sdk import SystemMessage, TaskStartedMessage

    from claude_dingtalk_bridge.claude_runner import TaskEvent

    started_acked = TaskStartedMessage(
        subtype="task_started", data={"subagent_type": "general-purpose"},
        task_id="t-acked", description="A", uuid="u1", session_id="s",
    )
    started_stuck = TaskStartedMessage(
        subtype="task_started", data={"subagent_type": "general-purpose"},
        task_id="t-stuck", description="B", uuid="u2", session_id="s",
    )
    ack_update = SystemMessage(
        subtype="task_updated",
        data={"task_id": "t-acked", "patch": {"status": "completed"}},
    )
    runner, events = await _run_turn_with(
        monkeypatch,
        [started_acked, started_stuck, ack_update, _result_message(),
         FakeSDKClient.HANG],
        timeout=0.05,
        settle=30.0,  # settle won't fire — t-stuck is not in acknowledged
    )
    timeouts = [
        e for e in events
        if isinstance(e, TaskEvent) and e.phase == "timeout"
    ]
    assert [e.task_id for e in timeouts] == ["t-stuck"]


async def test_drain_hard_timeout_silent_when_all_pending_acknowledged(
    monkeypatch,
):
    # Pathological config: settle is longer than the hard timeout, so the
    # outer _STUCK_TIMEOUT fires first even though every pending task has
    # been acknowledged. Since nothing is truly stuck, no `timeout` event
    # should be surfaced to the phone.
    from claude_agent_sdk import SystemMessage, TaskStartedMessage

    from claude_dingtalk_bridge.claude_runner import TaskEvent

    started = TaskStartedMessage(
        subtype="task_started", data={"subagent_type": "general-purpose"},
        task_id="t1", description="A", uuid="u", session_id="s",
    )
    ack_update = SystemMessage(
        subtype="task_updated",
        data={"task_id": "t1", "patch": {"status": "completed"}},
    )
    runner, events = await _run_turn_with(
        monkeypatch,
        [started, ack_update, _result_message(), FakeSDKClient.HANG],
        timeout=0.05,
        settle=30.0,
    )
    assert not any(
        isinstance(e, TaskEvent) and e.phase == "timeout" for e in events
    )


async def test_drain_settle_resets_on_each_incoming_message(monkeypatch):
    # The settle window must restart on every received message, so a stream
    # of late events (e.g. a relay turn for an acknowledged task) doesn't
    # get cut off mid-flight.
    from claude_agent_sdk import (
        AssistantMessage,
        SystemMessage,
        TaskStartedMessage,
        TextBlock,
    )

    from claude_dingtalk_bridge.claude_runner import TextEvent

    started = TaskStartedMessage(
        subtype="task_started", data={"subagent_type": "general-purpose"},
        task_id="t1", description="X", uuid="u", session_id="s",
    )
    ack_update = SystemMessage(
        subtype="task_updated",
        data={"task_id": "t1", "patch": {"status": "completed"}},
    )
    # After settle starts (post-ResultMessage), a late AssistantMessage
    # arrives mid-window — it must be processed (event emitted) before drain
    # exits at the next settle expiry.
    relay = AssistantMessage(content=[TextBlock("late relay")], model="opus")
    runner, events = await _run_turn_with(
        monkeypatch,
        [started, ack_update, _result_message(), relay, FakeSDKClient.HANG],
        timeout=30.0,
        settle=0.05,
    )
    assert any(
        isinstance(e, TextEvent) and e.text == "late relay" for e in events
    )


# --- defensive-rendering coverage -------------------------------------------

def test_summary_permission_denied_skips_none_and_empty_values():
    # The data dump filter drops None / "" so the rendered line stays signal-
    # only. Without it, every nullable SDK field would print as `field=None`.
    msg = SystemMessage(
        subtype="permission_denied",
        data={"tool_name": "Bash", "reason": None, "detail": ""},
    )
    summary = _sdk_message_summary(msg)
    assert "tool_name=Bash" in summary
    assert "reason=" not in summary
    assert "detail=" not in summary


def test_summary_init_without_cwd_renders_without_cwd_field():
    # Missing cwd in init data: the field is just dropped, the rest still
    # renders. Don't synthesise a placeholder.
    msg = SystemMessage(
        subtype="init",
        data={"model": "opus", "permissionMode": "auto", "claude_code_version": "1.0"},
    )
    summary = _sdk_message_summary(msg)
    assert "model=opus" in summary
    assert "cwd=" not in summary


def test_summary_system_message_with_unknown_subtype_returns_subtype_field():
    # A SystemMessage with a subtype we don't promote to a verb falls through
    # to a bare `subtype=<x>` rendering rather than crashing or returning None.
    msg = SystemMessage(subtype="some_future_subtype", data={})
    summary = _sdk_message_summary(msg)
    assert "subtype=some_future_subtype" in summary


def test_log_sdk_message_uses_generic_system_verb_for_unknown_subtype(caplog):
    # SystemMessage with a subtype we don't promote keeps the generic `system`
    # verb so the log line is still well-formed (verb + summary). The
    # `subtype=` field is NOT stripped (it isn't redundant with the verb).
    msg = SystemMessage(subtype="some_future_subtype", data={})
    with caplog.at_level(_logging.INFO, logger="claude_dingtalk_bridge.claude_runner"):
        _log_sdk_message(msg)
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == _logging.INFO]
    assert any(l.startswith("system ") for l in info_lines)
    assert any("subtype=some_future_subtype" in l for l in info_lines)


def test_log_sdk_message_uses_typename_verb_for_unknown_message(caplog):
    # A made-up SDK message type falls back to `<typename>.lower()` as the verb
    # and `-` as the summary — keeps the log column structure intact instead
    # of producing a malformed line.
    class _FutureSDKMessage:
        pass
    with caplog.at_level(_logging.INFO, logger="claude_dingtalk_bridge.claude_runner"):
        _log_sdk_message(_FutureSDKMessage())
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == _logging.INFO]
    assert any(l.startswith("_futuresdkmessage ") for l in info_lines)


def test_log_sdk_message_strips_subtype_exactly_when_summary_is_only_subtype(caplog):
    # _strip_subtype_if_redundant covers two cases: prefix-strip (already
    # tested via init), and exact-match (summary == "subtype=<x>") which
    # should reduce to an empty summary. Construct a compact_boundary with
    # nothing else to surface so its summary collapses to just `subtype=…`.
    # That doesn't naturally happen for compact_boundary (it always renders
    # pre_tokens/trigger too), so use a HookEventMessage whose hook_event_name
    # alone matches its subtype — actually the cleanest reproducer is a
    # SystemMessage with one of the known SUBTYPE_VERBS but empty data.
    msg = SystemMessage(subtype="permission_denied", data={})
    with caplog.at_level(_logging.INFO, logger="claude_dingtalk_bridge.claude_runner"):
        _log_sdk_message(msg)
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == _logging.INFO]
    # The verb already encodes the subtype; the trailing summary is empty.
    matching = [l for l in info_lines if l.startswith("permission_denied")]
    assert matching, info_lines
    assert matching[0].rstrip() == "permission_denied"


def test_summary_user_message_with_string_content_skips_iteration():
    # UserMessage.content can be a plain string (no tool results, no text
    # blocks) — the iteration is guarded by isinstance(list); the renderer
    # still emits a line with no tool_results / text_preview.
    msg = UserMessage(content="just a string")
    summary = _sdk_message_summary(msg)
    assert "tool_results=" not in summary
    assert "text_preview=" not in summary


def test_summary_user_message_text_blocks_render_combined_preview():
    # UserMessage with TextBlock content (skill output / system reminder
    # injections) — earlier versions rendered as a bare "-", losing the text.
    msg = UserMessage(content=[TextBlock("hello"), TextBlock("world")])
    summary = _sdk_message_summary(msg)
    assert 'text_preview="hello world"' in summary
    assert "text_len=10" in summary


def test_summary_assistant_message_with_thinking_continues_loop_to_next_block():
    # Branch coverage: ThinkingBlock followed by another block — the for loop
    # must continue to the next iteration after handling the ThinkingBlock,
    # not break out early.
    msg = AssistantMessage(
        content=[
            ThinkingBlock(thinking="deliberating", signature="sig"),
            TextBlock("done"),
        ],
        model="opus",
    )
    summary = _sdk_message_summary(msg)
    # Both contributed: thinking_len carries the first block, text_preview the
    # second.
    assert "thinking_len=" in summary
    assert 'text_preview="done"' in summary


def test_summary_assistant_message_skips_unknown_block_types():
    # ServerToolUseBlock (real SDK type) is not handled by the renderer — the
    # for loop just skips it and moves to the next block rather than crashing.
    # Forward-compat for future SDK block additions.
    from claude_agent_sdk import ServerToolUseBlock
    msg = AssistantMessage(
        content=[
            ServerToolUseBlock(id="srv_1", name="web_search", input={"q": "x"}),
            TextBlock("after server tool"),
        ],
        model="opus",
    )
    summary = _sdk_message_summary(msg)
    # The server tool block contributed nothing to the rendered fields —
    # without tool_entries the renderer takes the pure-text shortcut and just
    # emits the text. The point is the for loop didn't crash.
    assert "ServerToolUseBlock" not in summary
    assert "after server tool" in summary


def test_summary_user_message_skips_unknown_block_types():
    # Same forward-compat in UserMessage iteration.
    from claude_agent_sdk import ServerToolResultBlock
    msg = UserMessage(
        content=[
            ServerToolResultBlock(tool_use_id="srv_1", content="x"),
            TextBlock("after server result"),
        ],
    )
    summary = _sdk_message_summary(msg)
    assert "ServerToolResultBlock" not in summary
    assert 'text_preview="after server result"' in summary


def test_cancel_drain_when_not_draining_is_noop():
    # Documented no-op: callers (orchestrator) call cancel_drain() blindly
    # whenever a new prompt arrives; if no drain is in flight it must just
    # return rather than raising AttributeError.
    runner = ClaudeRunner()
    assert runner.is_draining is False
    runner.cancel_drain()  # must not raise
    assert runner.is_draining is False


async def test_run_turn_logs_warning_when_disconnect_raises(monkeypatch, caplog):
    # The teardown disconnect is wrapped in shield+wait_for+broad-except so a
    # hung or buggy SDK disconnect can't pin the task or crash the daemon.
    # When it does raise, we log a warning so the operator can see it
    # rather than letting it disappear silently.
    class _BadDisconnectClient(FakeSDKClient):
        async def disconnect(self):
            raise RuntimeError("disconnect blew up")

    monkeypatch.setattr(cr_mod, "ClaudeSDKClient", _BadDisconnectClient)
    FakeSDKClient.next_script = [_result_message()]
    runner = ClaudeRunner()
    events = []

    async def emit(event):
        events.append(event)

    with caplog.at_level(_logging.WARNING, logger="claude_dingtalk_bridge.claude_runner"):
        await runner.run_turn("/tmp/p", "go", emit)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= _logging.WARNING]
    assert any("disconnect did not complete cleanly" in m for m in warnings)


async def test_drain_returns_silently_when_stream_ends(monkeypatch):
    # If the SDK iterator exhausts mid-drain (StopAsyncIteration), the drain
    # must return cleanly with no timeout event — the stream is just done.
    from claude_agent_sdk import TaskStartedMessage
    started = TaskStartedMessage(
        subtype="task_started", data={"subagent_type": "general-purpose"},
        task_id="t1", description="X", uuid="u", session_id="s",
    )
    # No HANG after the result — the iterator runs out as soon as drain
    # tries to read the next message.
    runner, events = await _run_turn_with(
        monkeypatch,
        [started, _result_message()],
        timeout=5.0,
        settle=0.05,
    )
    # Pending task without a notification — drain saw the iterator end,
    # returned without fabricating a timeout event.
    from claude_dingtalk_bridge.claude_runner import TaskEvent
    assert not any(
        isinstance(e, TaskEvent) and e.phase == "timeout" for e in events
    )


async def test_drain_returns_when_cancel_set_after_consume(monkeypatch):
    # Race arm: cancel_drain() can land *during* a _consume() call (the
    # consume itself has await points). After consume returns, drain checks
    # _drain_cancel.is_set() and exits cleanly, even though the iterator
    # would still yield more messages.
    from claude_agent_sdk import TaskStartedMessage
    from claude_dingtalk_bridge.claude_runner import TaskEvent

    started = TaskStartedMessage(
        subtype="task_started", data={"subagent_type": "general-purpose"},
        task_id="t1", description="X", uuid="u", session_id="s",
    )
    # Trigger the cancel from inside the emit callback — that runs from
    # _consume, so by the time consume returns the cancel flag is set.
    monkeypatch.setattr(cr_mod, "ClaudeSDKClient", FakeSDKClient)
    monkeypatch.setattr(cr_mod, "_STUCK_TIMEOUT", 5.0)
    monkeypatch.setattr(cr_mod, "_SETTLE_TIMEOUT", 0.5)
    # After the result message, queue a notification followed by HANG — the
    # cancel must fire on the notification and prevent reading HANG.
    from claude_agent_sdk import TaskNotificationMessage
    notification = TaskNotificationMessage(
        subtype="task_notification", data={},
        task_id="t1", status="completed", output_file="/tmp/o",
        summary="done", uuid="u2", session_id="s",
        tool_use_id=None,
    )
    FakeSDKClient.next_script = [
        started, _result_message(), notification, FakeSDKClient.HANG,
    ]
    runner = ClaudeRunner()
    events: list = []

    async def emit(event):
        events.append(event)
        # Once the notification lands in drain, fire cancel — the drain loop's
        # post-consume cancel check then exits without reading HANG.
        if isinstance(event, TaskEvent) and event.phase == "notification":
            runner.cancel_drain()

    await asyncio.wait_for(runner.run_turn("/tmp/p", "go", emit), timeout=2.0)
    # No timeout event despite HANG being in the script — cancel won the race.
    assert not any(
        isinstance(e, TaskEvent) and e.phase == "timeout" for e in events
    )


def _perm_rules() -> PermissionRules:
    return PermissionRules(deny=["Bash(rm -rf:*)"])


def test_build_options_writes_settings_file_and_passes_path(tmp_path):
    runner = ClaudeRunner()
    runner.permission_rules = _perm_rules()
    runner.settings_file_path = tmp_path / "perms.json"
    options = runner._build_options("/Users/me/proj")
    assert options.settings == str(tmp_path / "perms.json")
    payload = json.loads((tmp_path / "perms.json").read_text())
    allow = payload["permissions"]["allow"]
    assert "Edit(/Users/me/proj/**)" in allow
    assert payload["permissions"]["deny"] == ["Bash(rm -rf:*)"]


def test_build_options_registers_pretooluse_hook(tmp_path):
    runner = ClaudeRunner()
    runner.permission_rules = _perm_rules()
    runner.settings_file_path = tmp_path / "perms.json"
    options = runner._build_options("/Users/me/proj")
    assert options.hooks is not None
    matchers = options.hooks.get("PreToolUse") or []
    assert any(m.matcher == "Bash" for m in matchers), (
        "Bridge must register a PreToolUse hook matched on Bash for the "
        "metacharacter check; without it, settings.json allow rules can "
        "short-circuit the bridge's escalation."
    )


def test_build_options_regenerates_settings_per_cwd(tmp_path):
    runner = ClaudeRunner()
    runner.permission_rules = _perm_rules()
    runner.settings_file_path = tmp_path / "perms.json"
    runner._build_options("/Users/me/proj-A")
    a = json.loads((tmp_path / "perms.json").read_text())
    runner._build_options("/Users/me/proj-B")
    b = json.loads((tmp_path / "perms.json").read_text())
    assert "Edit(/Users/me/proj-A/**)" in a["permissions"]["allow"]
    assert "Edit(/Users/me/proj-A/**)" not in b["permissions"]["allow"]
    assert "Edit(/Users/me/proj-B/**)" in b["permissions"]["allow"]
