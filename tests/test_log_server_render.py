"""Tests for the structured log renderer in scripts/log_server.py."""

import sys
from pathlib import Path

# scripts/ is not on the default pythonpath (pyproject pythonpath=["src"]),
# so inject it before importing the module under test.
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import log_server  # noqa: E402


def test_render_entry_unknown_module_falls_back_to_raw():
    html = log_server.render_entry(
        idx=1,
        ts="2026-05-25 10:00:00",
        level="INFO",
        module="some_unknown_module",
        rest="hello world",
    )
    assert "hello world" in html
    assert 'class="entry' in html


def test_sdk_message_branch_still_renders_typed_tree():
    rest = (
        "sdk_message AssistantMessage "
        "AssistantMessage(content=[TextBlock(text='hi')], model='claude-opus-4-7')"
    )
    html = log_server.render_entry(1, "2026-05-25 10:00:00", "INFO", "claude_runner", rest)
    # The SDK renderer wraps each typed call in a <details class="obj t-...">.
    assert "details" in html
    assert "AssistantMessage" in html
    assert "t-assistant" in html  # type class still applied


# ── Helper 1: format_duration_ms ──────────────────────────────────────────

def test_format_duration_ms_seconds():
    assert log_server.format_duration_ms(45538) == "45.5s"
    assert log_server.format_duration_ms(999) == "1.0s"
    assert log_server.format_duration_ms(0) == "0.0s"


def test_format_duration_ms_minutes():
    assert log_server.format_duration_ms(60_000) == "1m0s"
    assert log_server.format_duration_ms(210_058) == "3m30s"
    assert log_server.format_duration_ms(91_955) == "1m31s"


# ── Helper 2: detect_truncated ────────────────────────────────────────────

def test_detect_truncated_recognizes_ellipsis():
    assert log_server.detect_truncated("hello world…") == ("hello world", True)
    assert log_server.detect_truncated("…") == ("", True)


def test_detect_truncated_passes_through_normal_text():
    assert log_server.detect_truncated("hello world") == ("hello world", False)
    assert log_server.detect_truncated("") == ("", False)
    # ASCII "..." should NOT be treated as truncation marker — only U+2026.
    assert log_server.detect_truncated("hello...") == ("hello...", False)


# ── Helper 3: session_color + SESSION_PALETTE ─────────────────────────────

def test_session_color_stable_per_session():
    a1 = log_server.session_color("03cdbc4a")
    a2 = log_server.session_color("03cdbc4a")
    assert a1 == a2
    assert a1 in log_server.SESSION_PALETTE


def test_session_color_returns_none_for_missing():
    assert log_server.session_color(None) is None
    assert log_server.session_color("") is None
    assert log_server.session_color("-") is None


def test_session_color_uses_palette_only():
    samples = ["03cdbc4a", "0bb654d9", "71eb0446", "c5959ce1",
               "f3ef2e6a", "8e558b0a", "d948e6a5", "f6130796"]
    for s in samples:
        assert log_server.session_color(s) in log_server.SESSION_PALETTE


# ── Helper 4: parse_kv_args ───────────────────────────────────────────────

def test_parse_kv_args_simple_pairs():
    got = log_server.parse_kv_args("model=claude-opus-4-7 cwd=. permission_mode=auto")
    assert got == {"model": "claude-opus-4-7", "cwd": ".", "permission_mode": "auto"}


def test_parse_kv_args_double_quoted_string():
    got = log_server.parse_kv_args('project=dingtalk dry_run=False prompt="hello world"')
    assert got == {"project": "dingtalk", "dry_run": "False", "prompt": "hello world"}


def test_parse_kv_args_single_quoted_string():
    got = log_server.parse_kv_args("count=1 first='文件夹叫什么名字？'")
    assert got == {"count": "1", "first": "文件夹叫什么名字？"}


def test_parse_kv_args_bracketed_list():
    got = log_server.parse_kv_args(
        "cost=$0.30 permission_denials=[AskUserQuestion,Bash,Bash] subtype=success"
    )
    assert got["permission_denials"] == "[AskUserQuestion,Bash,Bash]"
    assert got["cost"] == "$0.30"
    assert got["subtype"] == "success"


def test_parse_kv_args_multiword_bareword_runs_to_next_key():
    # decision_reason has spaces and runs until ` message=` boundary.
    s = (
        "tool_name=Bash decision_reason_type=classifier "
        "decision_reason=Creating a folder outside the project scope "
        "message=Permission denied"
    )
    got = log_server.parse_kv_args(s)
    assert got["tool_name"] == "Bash"
    assert got["decision_reason_type"] == "classifier"
    assert got["decision_reason"] == "Creating a folder outside the project scope"
    assert got["message"] == "Permission denied"


def test_parse_kv_args_last_bareword_to_eol():
    got = log_server.parse_kv_args("subtype=api_retry cache_ttl_policy=1h")
    assert got == {"subtype": "api_retry", "cache_ttl_policy": "1h"}


# ── Helper 5: parse_tool_list ─────────────────────────────────────────────

def test_parse_tool_list_single_tool():
    got = log_server.parse_tool_list("[Bash#01F4oD82(mkdir ~/Desktop/untitled && ls -ld ~)]")
    assert got == [("Bash", "01F4oD82", "mkdir ~/Desktop/untitled && ls -ld ~")]


def test_parse_tool_list_args_with_commas_and_parens():
    got = log_server.parse_tool_list(
        "[Grep#01Xx1uUn(daemon-install|xcode|swift|xcrun in .)]"
    )
    assert got == [("Grep", "01Xx1uUn", "daemon-install|xcode|swift|xcrun in .")]


def test_parse_tool_list_two_tools():
    got = log_server.parse_tool_list(
        "[Read#01FJyTg7(sessions.py), Glob#011yGLG1(**/sessions.py)]"
    )
    assert got == [
        ("Read", "01FJyTg7", "sessions.py"),
        ("Glob", "011yGLG1", "**/sessions.py"),
    ]


def test_parse_tool_list_handles_empty_or_garbage():
    assert log_server.parse_tool_list("[]") == []
    assert log_server.parse_tool_list("not a list") == []


# ── Helper 6: parse_result_status ────────────────────────────────────────

def test_parse_result_status_done():
    got = log_server.parse_result_status("done 2.9s")
    assert got == {"status": "done", "duration": "2.9s", "msg": None, "content_len": None}


def test_parse_result_status_err_with_message():
    got = log_server.parse_result_status(
        "err 5.4s: Permission for this action was denied by the auto mode classifier"
    )
    assert got["status"] == "err"
    assert got["duration"] == "5.4s"
    assert "Permission for this action" in got["msg"]
    assert got["content_len"] is None


def test_parse_result_status_err_with_content_len():
    got = log_server.parse_result_status(
        "err 5.4s: Permission denied. R… content_len=1050"
    )
    assert got["status"] == "err"
    assert got["content_len"] == "1050"
    # message strips the `content_len=` tail.
    assert "content_len" not in got["msg"]


def test_parse_result_status_err_with_colon_but_empty_msg():
    # Defensive: if the daemon ever writes `err X.Xs:` with nothing after,
    # msg should be None, not "".
    got = log_server.parse_result_status("err 1.5s:")
    assert got["status"] == "err"
    assert got["duration"] == "1.5s"
    assert got["msg"] is None


def test_render_entry_extracts_session_and_emits_tint():
    html = log_server.render_entry(
        idx=5,
        ts="2026-05-25 11:48:20",
        level="INFO",
        module="claude_runner",
        rest="session=03cdbc4a turn=1 init model=claude-opus-4-7 cwd=. permission_mode=auto",
    )
    # session text preserved verbatim (no abbreviation).
    assert "session=" in html
    assert "03cdbc4a" in html
    assert "turn=" in html
    # session tint inline style is set.
    assert "--session-tint:" in html
    # Full timestamp displayed (NOT just HH:MM:SS).
    assert "2026-05-25 11:48:20" in html


def test_render_entry_no_session_no_tint_style():
    html = log_server.render_entry(
        idx=1,
        ts="2026-05-25 10:00:00",
        level="INFO",
        module="daemon",
        rest="Starting DingTalk stream client",
    )
    assert "--session-tint:" not in html
    assert "Starting DingTalk stream client" in html  # raw fallback


def test_render_entry_strips_session_dash_and_turn_prefix():
    # `session=-` should not produce a tint (treated as missing).
    html = log_server.render_entry(
        1, "2026-05-25 10:00:00", "INFO", "orchestrator",
        "session=- turn=1 ask_user_question count=1 first='hi'",
    )
    assert "--session-tint:" not in html
    # ask_user_question now renders as a structured chip, not raw text.
    assert "ask user" in html.lower()
    assert "hi" in html


def test_verb_orchestrator_running_turn_emits_prompt():
    html = log_server.render_entry(
        4, "2026-05-25 11:48:17", "INFO", "orchestrator",
        'Running turn 1: project=dingtalk dry_run=False prompt="在 ~/Desktop/ 创建一个文件夹"',
    )
    assert "chip user" in html
    assert "prompt-text" in html
    assert "在 ~/Desktop/ 创建一个文件夹" in html
    assert "turn 1" in html.lower()
    assert "dingtalk" in html


def test_verb_ask_user_question_pending():
    html = log_server.render_entry(
        8, "2026-05-25 11:48:31", "INFO", "orchestrator",
        "session=- turn=1 ask_user_question count=1 first='文件夹叫什么名字？'",
    )
    # ask pending — solid yellow (primary "needs human" event)
    assert "chip ask" in html
    assert "chip ask-out" not in html
    assert "ask user" in html.lower()
    assert "文件夹叫什么名字？" in html


def test_verb_ask_user_question_answered():
    html = log_server.render_entry(
        9, "2026-05-25 11:48:39", "INFO", "orchestrator",
        "session=- turn=1 ask_user_question answered count=1 waited=8.3s",
    )
    # ask answered — yellow outline (follow-up to the solid pending chip)
    assert "chip ask-out" in html
    assert "answered" in html.lower()
    assert "8.3s" in html


def test_verb_turn_interrupted():
    html = log_server.render_entry(
        189, "2026-05-25 13:45:55", "INFO", "orchestrator",
        "turn interrupted reason=user_stop",
    )
    # interrupted is a critical event — solid red.
    assert "chip crit" in html
    assert "interrupted" in html.lower()
    assert "user_stop" in html


def test_verb_claude_runner_init():
    html = log_server.render_entry(
        5, "2026-05-25 11:48:20", "INFO", "claude_runner",
        "session=03cdbc4a turn=1 init model=claude-opus-4-7[1m] cwd=. "
        "permission_mode=auto version=2.1.146 cache_ttl_policy=1h",
    )
    # init is now part of the system / plumbing family (gray + t-low dimmed).
    assert "chip sys" in html
    assert "t-low" in html
    assert "claude-opus-4-7[1m]" in html
    assert "auto" in html
    assert "2.1.146" in html


def test_verb_assistant_tools_single():
    html = log_server.render_entry(
        11, "2026-05-25 11:48:43", "INFO", "claude_runner",
        "session=03cdbc4a turn=1 assistant "
        "tools=[Bash#01F4oD82(mkdir ~/Desktop/untitled && ls -ld ~)] "
        "model=claude-opus-4-7",
    )
    assert "chip tool" in html
    assert "Bash" in html
    assert "01F4oD82" in html
    assert "mkdir ~/Desktop/untitled" in html


def test_verb_assistant_tools_multiple():
    html = log_server.render_entry(
        100, "2026-05-25 12:00:00", "INFO", "claude_runner",
        "session=03cdbc4a turn=1 assistant "
        "tools=[Read#01A(README.md), Glob#01B(**/*.py)] model=claude-opus-4-7",
    )
    assert "Read" in html and "Glob" in html
    assert "README.md" in html and "**/*.py" in html


def test_verb_thinking_new_daemon_format():
    # Updated daemons label thinking-only AssistantMessages with verb `thinking`.
    html = log_server.render_entry(
        14, "2026-05-25 11:48:52", "INFO", "claude_runner",
        "session=03cdbc4a turn=1 thinking input=1 output=50 cache_read=35.5K "
        "hit=98.7% model=claude-opus-4-7",
    )
    # Thinking is part of the assistant-reply family — solid blue `chip reply`.
    assert "chip reply" in html
    assert "thinking" in html
    assert "98.7%" in html
    assert "35.5K" in html
    assert "t-low" not in html


def test_verb_thinking_legacy_assistant_input_format():
    # Older daemons (pre-`thinking`-verb) emitted the same SDK event as
    # `assistant input=…`. Route those to the same renderer so historical
    # entries get the same chip as new ones.
    html = log_server.render_entry(
        14, "2026-05-25 11:48:52", "INFO", "claude_runner",
        "session=03cdbc4a turn=1 assistant input=1 output=50 cache_read=35.5K "
        "hit=98.7% model=claude-opus-4-7",
    )
    assert "chip reply" in html
    assert "thinking" in html
    assert "98.7%" in html


def test_verb_assistant_text_truncated():
    html = log_server.render_entry(
        19, "2026-05-25 11:49:06", "INFO", "claude_runner",
        'session=03cdbc4a turn=1 assistant "auto mode 分类器拒绝在 `~/Desktop/`…"',
    )
    assert "chip reply" in html
    assert "assistant-text" in html
    assert "auto mode 分类器拒绝" in html
    assert "truncated" in html  # trailing U+2026 detected


def test_verb_assistant_text_not_truncated():
    html = log_server.render_entry(
        15, "2026-05-25 11:48:31", "INFO", "claude_runner",
        'session=03cdbc4a turn=1 assistant "已创建 `~/Desktop/untitled/`。"',
    )
    assert "assistant-text" in html
    assert "已创建" in html
    assert "truncated" not in html


def test_verb_assistant_meta_error_promotes_to_crit():
    # Synthetic API-error reply: text_len + text_preview + stop_reason + error.
    # Previously fell through to raw <pre>; now routed to assistant_meta and
    # rendered with the critical (red) chip family so the operator can spot
    # 5xx/throttle responses scrolling past.
    html = log_server.render_entry(
        1, "2026-05-25 16:45:56", "INFO", "claude_runner",
        'session=f3ef2e6a turn=1 assistant text_len=139 '
        'text_preview="API Error: 529 Overloaded. This is a server-side issue, '
        'usually temporary — try …" model=<synthetic> stop_reason=stop_sequence '
        'error=server_error',
    )
    assert "chip crit" in html
    assert "assistant error" in html
    assert "stop_reason=" in html
    assert "stop_sequence" in html
    assert "server_error" in html
    assert "API Error: 529" in html
    # Body still shows the preview so the operator can see *why* it failed.
    assert "assistant-text" in html


def test_verb_assistant_meta_no_error_stays_reply():
    # Same branch but no error — stays in the reply (blue) family.
    html = log_server.render_entry(
        1, "2026-05-25 16:45:56", "INFO", "claude_runner",
        'session=f3ef2e6a turn=1 assistant text_len=42 '
        'text_preview="all done" model=claude-opus-4-7 stop_reason=end_turn',
    )
    assert "chip reply" in html
    assert "chip crit" not in html
    assert "stop_reason=" in html
    assert "end_turn" in html


def test_verb_user_text_hides_text_len_for_short_payloads():
    # The text_len kv on the header is a magnitude signal; suppress it when
    # the preview already shows essentially the whole payload (<= 200 chars).
    html = log_server.render_entry(
        1, "2026-05-25 10:00:00", "INFO", "claude_runner",
        'session=03cdbc4a turn=1 user text_len=42 text_preview="short reminder"',
    )
    assert '<span class="chip reply-out">text</span>' in html
    assert "short reminder" in html
    # The kv chip should not appear for the small text_len. data-raw still
    # carries the verbatim line for ctrl+click; check only the visible part.
    import re as _re
    visible = _re.sub(r'data-raw="[^"]*"', "", html)
    assert "text_len=" not in visible
    assert ">42<" not in visible


def test_verb_user_text_shows_text_len_for_large_payloads():
    # >200 chars: the 80-char preview hides magnitude; surface the count.
    html = log_server.render_entry(
        1, "2026-05-25 10:00:00", "INFO", "claude_runner",
        'session=03cdbc4a turn=1 user text_len=3522 text_preview="…"',
    )
    assert '<span class="chip reply-out">text</span>' in html
    assert "text_len=" in html
    assert "3522" in html


def test_verb_tool_results_done():
    html = log_server.render_entry(
        25, "2026-05-25 11:51:29", "INFO", "claude_runner",
        "session=03cdbc4a turn=2 user tool_results=[Bash#01RyfTbS(done 2.9s)]",
    )
    # tool_results sits in the tool family — green outline, matching the
    # solid green of the preceding `tool call` so the pair reads as a unit.
    assert "chip tool-out" in html
    assert "Bash" in html
    assert "01RyfTbS" in html
    assert "2.9s" in html
    assert "duration done" in html


def test_verb_tool_results_err_with_truncated_msg_and_content_len():
    html = log_server.render_entry(
        13, "2026-05-25 11:48:49", "INFO", "claude_runner",
        "session=03cdbc4a turn=1 user "
        "tool_results=[Bash#01F4oD82(err 5.4s: Permission for this action "
        "was denied by the Claude Code auto mode classifier. Reason: "
        "Creating a directory outside the project scope (~/Desktop/) with "
        "an agent-inferred name without explicit user con… content_len=1050)]",
    )
    assert "duration err" in html
    assert "5.4s" in html
    assert "Permission for this action" in html
    assert "1050" in html
    assert "truncated" in html


def test_verb_tool_results_ask_user_question_answered_renders_as_done():
    # New daemon log format: AskUserQuestion tool_result carries status
    # `answered` (set by the daemon's _ask_question_status). Render as a
    # green ✓ — visually distinguishable from a real error.
    html = log_server.render_entry(
        10, "2026-05-25 11:48:39", "INFO", "claude_runner",
        "session=03cdbc4a turn=1 user tool_results=[AskUserQuestion#01BnzX83("
        "answered 8.3s: The user answered your AskUserQuestion via DingTalk: "
        "- Folder name: untitled Continue based on these answers.)]",
    )
    assert "duration done" in html
    assert "answered" in html
    assert "8.3s" in html
    # No err styling sneaking in.
    assert "duration err" not in html


def test_verb_tool_results_ask_user_question_no_answer_renders_as_err():
    html = log_server.render_entry(
        11, "2026-05-25 12:00:00", "INFO", "claude_runner",
        "session=03cdbc4a turn=1 user tool_results=[AskUserQuestion#01XYZ("
        "no_answer 60.5s: The user did not answer your AskUserQuestion via DingTalk "
        "(cancelled or timed out). Do not retry; proceed with a reasonable default.)]",
    )
    assert "duration err" in html
    assert "no answer" in html
    assert "60.5s" in html


def test_verb_tool_results_ask_user_question_legacy_err_promoted_to_answered():
    # Pre-fix daemons logged the same SDK event as `err: The user answered ...`.
    # The viewer recognises the message prefix and promotes the status so
    # historical entries render the same as new ones.
    html = log_server.render_entry(
        10, "2026-05-25 11:48:39", "INFO", "claude_runner",
        "session=03cdbc4a turn=1 user tool_results=[AskUserQuestion#01BnzX83("
        "err 8.3s: The user answered your AskUserQuestion via DingTalk: "
        "- Folder name: untitled Continue based on these answers.)]",
    )
    assert "duration done" in html
    assert "answered" in html
    assert "duration err" not in html


def test_verb_tool_results_ask_user_question_legacy_err_promoted_to_no_answer():
    html = log_server.render_entry(
        12, "2026-05-25 12:00:00", "INFO", "claude_runner",
        "session=03cdbc4a turn=1 user tool_results=[AskUserQuestion#01XYZ("
        "err 60.5s: The user did not answer your AskUserQuestion via DingTalk "
        "(cancelled or timed out). Do not retry; proceed with a reasonable default.)]",
    )
    assert "duration err" in html
    assert "no answer" in html


def test_verb_permission_denied():
    html = log_server.render_entry(
        12, "2026-05-25 11:48:49", "INFO", "claude_runner",
        "session=03cdbc4a turn=1 permission_denied tool_name=Bash "
        "tool_use_id=toolu_01F4oD8231k1HudDFUne1FV3 "
        "decision_reason_type=classifier "
        "decision_reason=Creating a directory outside the project scope "
        "message=Permission denied by classifier",
    )
    assert "permission denied" in html.lower()
    assert "chip crit" in html
    assert "Bash" in html
    assert "classifier" in html
    assert "deny-block" in html
    assert "Permission denied by classifier" in html


def test_verb_result_success():
    html = log_server.render_entry(
        20, "2026-05-25 11:49:06", "INFO", "claude_runner",
        'session=03cdbc4a turn=1 result subtype=success duration_ms=45538 '
        'duration_api_ms=26227 num_turns=4 stop_reason=end_turn cost=$0.3031 '
        'permission_denials=[AskUserQuestion,Bash,Bash] '
        'result="auto mode 分类器拒绝在 `~/Desktop/` 创建文件夹…"',
    )
    # A successful turn closes in the assistant-reply family (solid blue),
    # labelled `turn done · success`. cost stays out of the visible header;
    # ctrl/cmd+click reveals the raw line if needed for accounting.
    assert "chip reply" in html
    assert "turn done · success" in html
    assert 'class="cost"' not in html
    assert "45.5s" in html
    assert "4 turns" in html
    assert "result-text" in html
    assert "auto mode 分类器" in html
    assert "truncated" in html


def test_verb_result_non_success_uses_crit_chip():
    html = log_server.render_entry(
        21, "2026-05-25 12:00:00", "INFO", "claude_runner",
        'session=03cdbc4a turn=1 result subtype=error_max_turns duration_ms=10000 '
        'num_turns=50 cost=$1.20',
    )
    # A non-success turn is a critical event — solid red.
    assert "chip crit" in html
    assert "turn err · error_max_turns" in html


def test_verb_turn_tokens_not_dimmed():
    html = log_server.render_entry(
        22, "2026-05-25 11:49:06", "INFO", "claude_runner",
        "session=03cdbc4a turn=1 turn tokens: input=9 output=850 "
        "cache_read=106.5K hit=74.4% write_1h=36.6K write_5m=0",
    )
    # turn tokens is the assistant-reply family's running tally — blue outline.
    assert "chip reply-out" in html
    assert "turn tokens" in html
    assert "850" in html
    assert "106.5K" in html
    assert "74.4%" in html
    assert "36.6K" in html
    assert "t-low" not in html
    assert "faint" not in html


def test_verb_task_started():
    html = log_server.render_entry(
        737, "2026-05-25 15:07:08", "INFO", "claude_runner",
        "session=f3ef2e6a turn=4 task_started task_id=ace1a56ff9b1ef60a "
        "subagent_type=general-purpose desc=Implement Task 1: log_render scaffold",
    )
    # task started — solid cyan
    assert "chip task" in html
    assert "task ▶" in html
    assert "task_id=" in html
    assert "ace1a56f" in html
    assert "subagent_type=" in html
    assert "general-purpose" in html
    assert "Implement Task 1" in html


def test_verb_task_notification_completed():
    html = log_server.render_entry(
        751, "2026-05-25 15:08:27", "INFO", "claude_runner",
        "session=f3ef2e6a turn=4 task_notification "
        "task_id=ace1a56ff9b1ef60a status=completed duration_ms=75646 "
        "tool_uses=6 total_tokens=27857",
    )
    # task notification — cyan outline (follow-up to the solid `task ▶`).
    # Every meta field shows its key (task_id / status / duration / tool_uses /
    # tokens) so the eye doesn't have to guess which bare number is which.
    # tool_uses keeps its source name to disambiguate from the assistant's
    # own `tools=[…]` list; tokens is run through format_tokens (27857→27.9K).
    assert "chip task-out" in html
    assert "task ●" in html
    assert "task_id=" in html
    assert "ace1a56f" in html
    assert "status=" in html
    assert "completed" in html
    assert "duration=" in html
    assert "1m15s" in html
    assert "tool_uses=" in html
    assert ">6<" in html
    assert "tokens=" in html
    assert "27.9K" in html
    # The raw integer survives in data-raw (the verbatim log line stash for
    # ctrl/cmd+click), but must not appear in any visible rendering. Strip
    # the data-raw attribute and re-check.
    import re as _re
    visible = _re.sub(r'data-raw="[^"]*"', "", html)
    assert "27857" not in visible


def test_verb_rate_limit_event():
    html = log_server.render_entry(
        1043, "2026-05-25 15:30:27", "INFO", "claude_runner",
        "session=f3ef2e6a turn=4 rate_limit_event status=allowed_warning "
        "type=five_hour utilization=95.0% resets_at=1779694200 (2026-05-25 15:30:00)",
    )
    # rate limit is a critical event — solid red, paired with permission_denied
    # and turn interruption.
    assert "chip crit" in html
    assert "rate limit" in html.lower()
    assert "95.0%" in html
    assert "five_hour" in html
    # Resets time is rendered in human-readable local form (kv-style),
    # never as `resets 1779694200 (...)` with the raw epoch up front.
    assert "resets=2026-05-25 15:30:00" in html
    assert "resets 1779694200" not in html


def test_verb_system_api_retry_low_priority():
    html = log_server.render_entry(
        1671, "2026-05-25 16:42:03", "INFO", "claude_runner",
        "session=f3ef2e6a turn=1 system subtype=api_retry cache_ttl_policy=1h",
    )
    assert "api_retry" in html
    assert "t-low" in html


def test_client_module_low_priority():
    html = log_server.render_entry(
        2, "2026-05-25 11:47:43", "INFO", "client",
        "open connection, url=https://api.dingtalk.com/...",
    )
    assert "t-low" in html
    assert "open connection" in html


def test_daemon_starting_low_priority():
    html = log_server.render_entry(
        1, "2026-05-25 11:47:43", "INFO", "daemon",
        "Starting DingTalk stream client",
    )
    assert "t-low" in html
    assert "Starting DingTalk stream client" in html


def test_verb_daemon_inbound_text():
    html = log_server.render_entry(
        1659, "2026-05-25 16:38:34", "INFO", "daemon",
        'inbound msgtype=text sender=manager8234 preview="hi"',
    )
    assert "chip user" in html
    assert "inbound" in html.lower()
    assert "text" in html
    assert "manager8234" in html
    assert "hi" in html


def test_subagent_prefix_assistant_tools_still_dispatches():
    # When a subagent issues a tool call, the daemon prepends
    # `agent=sub sub_id=… sub_type=…` after the verb. The dispatcher must
    # strip that segment so the inner verb (`tools=[…]`) still matches.
    html = log_server.render_entry(
        2049, "2026-05-25 20:35:51", "INFO", "claude_runner",
        "session=8e558b0a turn=7 assistant agent=sub sub_id=a6ea7111 "
        "sub_type=general-purpose tools=[Bash#011Eu7HU(git diff --stat HEAD)] "
        "model=claude-sonnet-4-6 parent_tool_use_id=01M9scVQ",
    )
    assert "chip tool" in html
    assert "Bash" in html
    assert "git diff --stat HEAD" in html
    # Sub annotation in the header so the entry pairs with its task ▶ line.
    assert "chip task-out" in html
    assert "sub · general-purpose · a6ea7111" in html
    # Indent class set so the entry visually nests under its parent task line.
    assert ' sub"' in html or " sub " in html
    assert "general-purpose" in html


def test_subagent_prefix_tool_results_still_dispatches():
    html = log_server.render_entry(
        2050, "2026-05-25 20:35:51", "INFO", "claude_runner",
        "session=8e558b0a turn=7 user agent=sub sub_id=a6ea7111 "
        "sub_type=general-purpose tool_results=[Bash#011Eu7HU(done 464ms)] "
        "parent_tool_use_id=01M9scVQ",
    )
    assert "chip tool-out" in html
    assert "duration done" in html
    assert "464ms" in html
    assert "chip task-out" in html
    assert "sub · general-purpose · a6ea7111" in html
    # Indent class set so the entry visually nests under its parent task line.
    assert ' sub"' in html or " sub " in html


def test_subagent_user_text_renders_as_user_text_verb():
    # A subagent's UserMessage carrying TextBlock content (Skill output / system
    # reminder injection / subagent report) — `user text_len=N text_preview=…`.
    html = log_server.render_entry(
        2048, "2026-05-25 20:35:46", "INFO", "claude_runner",
        "session=8e558b0a turn=7 user agent=sub sub_id=a6ea7111 "
        "sub_type=general-purpose text_len=3522 text_preview=\"You are reviewing whether…\" "
        "parent_tool_use_id=01M9scVQ",
    )
    assert '<span class="chip reply-out">text</span>' in html
    assert "chip reply-out" in html
    assert "3522" in html
    assert "You are reviewing whether" in html
    assert "chip task-out" in html
    assert "sub · general-purpose · a6ea7111" in html
    # Indent class set so the entry visually nests under its parent task line.
    assert ' sub"' in html or " sub " in html


def test_verb_daemon_inbound_picture_masked_sender():
    html = log_server.render_entry(
        1700, "2026-05-25 17:00:00", "INFO", "daemon",
        'inbound msgtype=picture sender=man******234 preview="<image>"',
    )
    assert "picture" in html
    assert "man******234" in html


# ─── SDK repr renderer coverage ──────────────────────────────────────────


def test_parse_repr_returns_none_on_syntax_error():
    # Malformed Python expression — parse_repr swallows SyntaxError and
    # returns None so _render_sdk_message falls back to a raw <pre>.
    assert log_server.parse_repr("AssistantMessage(content=[unclosed") is None


def test_try_json_parse_returns_none_on_invalid_json():
    # Returns None when first char doesn't look like JSON, or when json.loads
    # fails. Both branches preserve the surrounding renderer's fallback path.
    assert log_server.try_json_parse("not json at all") is None
    assert log_server.try_json_parse("") is None
    assert log_server.try_json_parse('{"unclosed": "object"') is None


def test_render_value_covers_all_primitives():
    # The whole leaf-rendering matrix in one place — None / bool / int / float
    # / empty list / dict / unknown type fallback.
    assert "lit null" in log_server.render_value(None)
    assert "lit bool" in log_server.render_value(True)
    assert "lit num" in log_server.render_value(42)
    assert "lit num" in log_server.render_value(3.14)
    assert "empty" in log_server.render_value([])
    items = log_server.render_value([1, 2, 3])
    assert "ol class" in items and ">1<" in items
    plain_dict = log_server.render_value({"a": 1})
    assert "table class=\"plain\"" in plain_dict
    # Unknown type → str() fallback wrapped in str-inline span.
    class _Opaque:
        def __str__(self):
            return "opaque-thing"
    assert "opaque-thing" in log_server.render_value(_Opaque())


def test_render_dict_empty_plain_dict():
    # No __type__ key + empty body → distinct "empty {}" span (not a table).
    out = log_server.render_dict({})
    assert "empty" in out and "{ }" in out


def test_render_string_pretty_prints_embedded_json():
    # A long-ish string whose key hint is in JSON_LIKELY_KEYS gets the
    # collapsible JSON tree treatment.
    payload = (
        '{"name": "Alice in Wonderland", "items": [1, 2, 3, 4, 5], '
        '"nested": {"k": "value with some content"}}'
    )
    assert len(payload) > 60  # precondition for the JSON branch
    out = log_server.render_string(payload, key_hint="output")
    assert "json-embed" in out
    assert "JSON," in out  # badge with byte count


def test_render_string_uses_pre_for_long_or_multiline():
    long_one = "a" * 250
    out = log_server.render_string(long_one)
    assert "pre class=\"str\"" in out
    out2 = log_server.render_string("first\nsecond")
    assert "pre class=\"str\"" in out2


def test_ast_to_obj_handles_tuple_set_dict_unaryop_and_constants():
    # Reach the less-common AST nodes (Tuple, Set, Dict, UnaryOp on non-num,
    # bare Name constants) by parsing a repr string that uses all of them.
    expr = "Foo(a=(1, 2), b={3, 4}, c={'k': True, 'm': None, 'n': False}, d=-1, e=-foo)"
    obj = log_server.parse_repr(expr)
    assert obj["__type__"] == "Foo"
    assert obj["a"] == [1, 2]                    # Tuple → list
    assert obj["b"] == [3, 4]                    # Set → list
    assert obj["c"] == {"k": True, "m": None, "n": False}  # Name constants
    assert obj["d"] == -1                         # UnaryOp on number
    assert obj["e"] == "-<foo>"                   # UnaryOp on non-number


def test_ast_to_obj_handles_call_with_positional_args():
    # Coverage for the `for i, arg in enumerate(node.args)` loop — typed reprs
    # are usually keyword-only but defensive parsing supports positionals too.
    obj = log_server.parse_repr("Wrapped('first', 42, kw='x')")
    assert obj["__type__"] == "Wrapped"
    assert obj["_arg0"] == "first"
    assert obj["_arg1"] == 42
    assert obj["kw"] == "x"


def test_ast_to_obj_unparseable_node_returns_marker():
    # An AST node type we don't handle yields a `<unparsed:…>` marker —
    # render_value will still show it as a plain string.
    import ast
    node = ast.parse("lambda x: x", mode="eval").body
    assert log_server.ast_to_obj(node).startswith("<unparsed:")


def test_sdk_message_unparseable_payload_falls_back_to_raw_pre():
    # When parse_repr returns None, _render_sdk_message uses a raw <pre>
    # rather than the typed tree.
    rest = "sdk_message AssistantMessage AssistantMessage(content=[unclosed"
    out = log_server.render_entry(1, "2026-05-25 10:00:00", "INFO", "claude_runner", rest)
    assert "pre class=\"raw\"" in out
    # The original payload text is preserved verbatim in the fallback.
    assert "AssistantMessage(content=[unclosed" in out


# ─── parse_kv_args edge cases ────────────────────────────────────────────


def test_parse_kv_args_empty_input():
    assert log_server.parse_kv_args("") == {}


def test_parse_kv_args_garbage_returns_empty_dict():
    # No `<word>=` token anywhere → loop breaks on the first `_KEY_RE.match`
    # without producing any pairs.
    assert log_server.parse_kv_args("hello world no equals here") == {}


def test_parse_kv_args_trailing_key_with_no_value_yields_empty_string():
    # `foo=` at the very end of input: the key is captured but value is "".
    assert log_server.parse_kv_args("foo=") == {"foo": ""}


def test_parse_kv_args_trailing_whitespace_after_quoted_exits_cleanly():
    # A quoted value advances `i` past the closing quote without breaking
    # the outer loop. Trailing whitespace then drives the next iteration
    # into the `if i >= n: break` exit (a bareword tail would have already
    # broken via the `nm is None` branch, so we need quoted or bracketed).
    assert log_server.parse_kv_args('a="hello"   ') == {"a": "hello"}
    assert log_server.parse_kv_args("a=[1,2,3]   ") == {"a": "[1,2,3]"}


def test_parse_kv_args_unclosed_quoted_value_runs_to_eol():
    # No matching closing quote → take everything after the opening quote.
    got = log_server.parse_kv_args('msg="unterminated string')
    assert got == {"msg": "unterminated string"}


def test_parse_kv_args_unclosed_bracket_value_runs_to_eol():
    got = log_server.parse_kv_args("items=[a,b,c without close")
    assert got == {"items": "[a,b,c without close"}


# ─── parse_tool_list / parse_result_status / _find_balanced_close ────────


def test_parse_tool_list_empty_brackets():
    assert log_server.parse_tool_list("[]") == []


def test_parse_tool_list_whitespace_only_inner_exits_cleanly():
    # `[   ]` and `[ , , ]` advance i past every space/comma without finding
    # a `Name#id(` head; the loop must hit the i >= n early-exit branch.
    assert log_server.parse_tool_list("[   ]") == []
    assert log_server.parse_tool_list("[ , , ]") == []


def test_parse_tool_list_inner_garbage_breaks_out():
    # Inner text doesn't match `Name#id(` — loop breaks, returning whatever
    # was parsed before. `[]` is malformed-but-bracketed, so we get `[]`.
    assert log_server.parse_tool_list("[no tool head here]") == []


def test_parse_result_status_unmatched_input():
    # Doesn't start with done/err/answered/no_answer — return all-None dict.
    got = log_server.parse_result_status("garbage")
    assert got == {"status": None, "duration": None, "msg": None, "content_len": None}


def test_find_balanced_close_unbalanced_returns_eol():
    # `[` with no matching `]` falls through to the safe end-of-string value
    # so _verb_assistant_tools doesn't crash on malformed input.
    assert log_server._find_balanced_close("[a, b, c", 0) == len("[a, b, c") - 1


# ─── verb sub-renderer guard rails ───────────────────────────────────────


def test_verb_assistant_tools_returns_none_when_prefix_missing():
    # Defensive guard inside the verb: if the dispatcher ever hands it a
    # tail that doesn't start with `tools=`, return None so the entry
    # falls back to raw rather than emitting a broken card.
    assert log_server._verb_assistant_tools("not-tools=stuff") is None


def test_verb_user_text_handles_text_preview_without_quotes():
    # text_preview is normally `"..."`-wrapped; if it isn't, the helper
    # must still render the value rather than crash on the slice.
    html = log_server.render_entry(
        1, "2026-05-25 10:00:00", "INFO", "claude_runner",
        "session=03cdbc4a turn=1 user text_len=10 text_preview=raw-no-quotes",
    )
    assert '<span class="chip reply-out">text</span>' in html
    assert "raw-no-quotes" in html


def test_verb_tool_results_returns_none_when_prefix_missing():
    assert log_server._verb_tool_results("nothing-here") is None


def test_verb_tool_results_unknown_status_falls_through_to_plain_duration():
    # A status the regex doesn't recognise (e.g., a future SDK adds a new
    # state) renders as a bare `<span class="duration">…</span>` rather
    # than greenlighting it as success or failure.
    html = log_server.render_entry(
        1, "2026-05-25 10:00:00", "INFO", "claude_runner",
        "session=03cdbc4a turn=1 user tool_results=[Bash#01ABCDEF(pending 0.5s)]",
    )
    assert "duration" in html
    # Not classified as either success or error.
    assert "duration done" not in html
    assert "duration err" not in html


def test_data_raw_attribute_carries_full_log_line():
    # ctrl/cmd+click toggle reads data-raw from the article; it must contain
    # the full original line (timestamp + level + module + rest, verbatim).
    html = log_server.render_entry(
        5, "2026-05-25 11:48:20", "INFO", "claude_runner",
        "session=03cdbc4a turn=1 init model=claude-opus-4-7 cwd=. permission_mode=auto",
    )
    assert 'data-raw="' in html
    assert "2026-05-25 11:48:20 INFO claude_runner session=03cdbc4a" in html


def test_data_raw_escapes_html_special_chars():
    # The raw line may contain quotes and angle brackets; data-raw is in an
    # HTML attribute, so they must be entity-escaped.
    html = log_server.render_entry(
        1, "2026-05-25 10:00:00", "INFO", "claude_runner",
        'session=03cdbc4a turn=1 assistant "<dangerous> & \'quote\'"',
    )
    # No raw `"`, `<`, or `&` inside the data-raw attribute value.
    import re as _re
    m = _re.search(r'data-raw="([^"]*)"', html)
    assert m is not None
    raw = m.group(1)
    assert "<dangerous>" not in raw  # was entity-escaped
    assert "&lt;dangerous&gt;" in raw
    assert "&amp;" in raw
    assert "&#x27;" in raw or "&#39;" in raw  # single quote escaped
