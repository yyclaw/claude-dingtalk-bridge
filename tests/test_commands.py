import pytest

from claude_dingtalk_bridge.commands import CommandType, parse_command


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/stop", CommandType.STOP),
        ("/ls", CommandType.LIST_PROJECTS),
        ("/status", CommandType.STATUS),
        ("/pwd", CommandType.PWD),
        ("/clear", CommandType.CLEAR),
        ("/help", CommandType.HELP),
        ("/update", CommandType.UPDATE),
        ("/UPDATE", CommandType.UPDATE),
        ("/STATUS", CommandType.STATUS),
        ("ok", CommandType.APPROVE),
        ("YES", CommandType.APPROVE),
        ("Approve", CommandType.APPROVE),
        ("\U0001f44c", CommandType.APPROVE),  # 👌
        ("\U0001f44c\U0001f3fb", CommandType.APPROVE),  # 👌🏻 light skin
        ("\U0001f44c\U0001f3ff", CommandType.APPROVE),  # 👌🏿 dark skin
        ("  \U0001f44c\uFE0F  ", CommandType.APPROVE),  # padded 👌 + VS16
        ("no", CommandType.DENY),
        ("deny", CommandType.DENY),
        ("reject", CommandType.DENY),
        ("❌", CommandType.DENY),  # ❌
        ("  \u274c\uFE0F  ", CommandType.DENY),  # padded ❌ + VS16
    ],
)
def test_keyword_commands(text, expected):
    assert parse_command(text).type == expected


def test_keyword_is_whitespace_tolerant():
    assert parse_command("  /stop  ").type == CommandType.STOP


@pytest.mark.parametrize(
    "text,ctype,arg",
    [
        ("/verbose", CommandType.VERBOSE, None),
        ("/verbose on", CommandType.VERBOSE, "on"),
        ("/verbose off", CommandType.VERBOSE, "off"),
        ("/debug", CommandType.DEBUG, None),
        ("/debug on", CommandType.DEBUG, "on"),
        ("/debug OFF", CommandType.DEBUG, "OFF"),
    ],
)
def test_on_off_commands(text, ctype, arg):
    cmd = parse_command(text)
    assert cmd.type == ctype
    assert cmd.arg == arg


def test_switch_project():
    cmd = parse_command("/cd multica")
    assert cmd.type == CommandType.SWITCH_PROJECT
    assert cmd.arg == "multica"


def test_switch_project_without_arg():
    cmd = parse_command("/cd")
    assert cmd.type == CommandType.SWITCH_PROJECT
    assert cmd.arg is None


def test_unknown_slash_command():
    for text in ("/statu", "/brief", "/nodebug"):
        cmd = parse_command(text)
        assert cmd.type == CommandType.UNKNOWN
        assert cmd.arg == text


def test_plain_text_is_prompt():
    cmd = parse_command("帮我修复登录页的 bug")
    assert cmd.type == CommandType.PROMPT
    assert cmd.arg == "帮我修复登录页的 bug"


def test_keyword_as_prefix_of_longer_text_is_prompt():
    cmd = parse_command("stop the world")
    assert cmd.type == CommandType.PROMPT
    assert cmd.arg == "stop the world"


def test_chinese_keywords_no_longer_recognized():
    for text in ("停", "调试", "项目", "状态", "重置"):
        assert parse_command(text).type == CommandType.PROMPT


def test_unmapped_emoji_is_prompt():
    # Only 👌/❌ are reply aliases; other emoji stay prompts.
    for text in ("\U0001f44d", "\U0001f44e", "✅"):  # 👍 👎 ✅
        assert parse_command(text).type == CommandType.PROMPT


def test_reply_emoji_inside_text_is_prompt():
    # The alias only fires on a bare emoji, not one embedded in a sentence.
    cmd = parse_command("\U0001f44c looks good")  # 👌 looks good
    assert cmd.type == CommandType.PROMPT


def test_session_command():
    assert parse_command("/session").type == CommandType.SESSION


def test_resume_without_arg():
    cmd = parse_command("/resume")
    assert cmd.type == CommandType.RESUME
    assert cmd.arg is None


def test_resume_with_number():
    cmd = parse_command("/resume 3")
    assert cmd.type == CommandType.RESUME
    assert cmd.arg == "3"


def test_resume_with_uuid():
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    cmd = parse_command(f"/resume {uuid}")
    assert cmd.type == CommandType.RESUME
    assert cmd.arg == uuid


@pytest.mark.parametrize(
    "text",
    ["/compact", "/context", "/COMPACT", "/compact be brief", "  /compact  "],
)
def test_passthrough_commands_parse_as_prompt(text):
    cmd = parse_command(text)
    assert cmd.type == CommandType.PROMPT
    assert cmd.arg == text.strip()


@pytest.mark.parametrize(
    "text,arg",
    [
        ("/model", None),
        ("/model 2", "2"),
        ("/model sonnet", "sonnet"),
        ("/MODEL", None),
    ],
)
def test_model_command_parses(text, arg):
    cmd = parse_command(text)
    assert cmd.type == CommandType.MODEL
    assert cmd.arg == arg


def test_model_choices_names():
    from claude_dingtalk_bridge.orchestrator import MODEL_NAMES

    assert MODEL_NAMES == ("opus", "sonnet", "haiku")


@pytest.mark.parametrize(
    "text,arg",
    [
        ("/stop", None),
        ("/stop all", "all"),
        ("/stop ALL", "ALL"),
        ("  /stop  all  ", "all"),
    ],
)
def test_stop_command_parses_optional_arg(text, arg):
    cmd = parse_command(text)
    assert cmd.type == CommandType.STOP
    assert cmd.arg == arg


@pytest.mark.parametrize(
    "text,arg",
    [
        ("/queue", None),
        ("/queue rm 2", "rm 2"),
        ("/queue rm all", "rm all"),
        ("/queue clear", "clear"),
        ("/QUEUE", None),
        ("  /queue   rm 3 ", "rm 3"),
    ],
)
def test_queue_command_parses(text, arg):
    cmd = parse_command(text)
    assert cmd.type == CommandType.QUEUE
    assert cmd.arg == arg


@pytest.mark.parametrize(
    "text,arg",
    [
        ("/help", None),
        ("/help queue", "queue"),
        ("/help /queue", "/queue"),
        ("/HELP resume", "resume"),
    ],
)
def test_help_command_parses_optional_arg(text, arg):
    cmd = parse_command(text)
    assert cmd.type == CommandType.HELP
    assert cmd.arg == arg


def test_every_command_has_a_help_entry():
    """Each user-facing slash command must be documented in HELP so that
    /help <cmd> and the /help list never reference a missing entry."""
    from claude_dingtalk_bridge.commands import (
        HELP,
        _ARG_COMMANDS,
        _PASSTHROUGH_COMMANDS,
        _SLASH_KEYWORDS,
    )

    documented = set(HELP)
    for name in (*_SLASH_KEYWORDS, *_ARG_COMMANDS, *_PASSTHROUGH_COMMANDS):
        assert name.lstrip("/") in documented, f"{name} missing from HELP"


def test_help_entry_details_keep_bullets_on_separate_lines():
    """A multi-bullet detail must not concatenate two bullets onto one line —
    adjacent string literals without a trailing `\\n` silently merge them."""
    from claude_dingtalk_bridge.commands import HELP

    for name, entry in HELP.items():
        if entry.detail is None:
            continue
        for line in entry.detail.split("\n"):
            assert line.count("- `/") <= 1, (
                f"HELP[{name!r}] detail merges two bullets: {line!r}"
            )
