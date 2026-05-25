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
        ("/STATUS", CommandType.STATUS),
        ("ok", CommandType.APPROVE),
        ("YES", CommandType.APPROVE),
        ("Approve", CommandType.APPROVE),
        ("no", CommandType.DENY),
        ("deny", CommandType.DENY),
        ("reject", CommandType.DENY),
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
    ["/compact", "/context", "/usage", "/COMPACT", "/compact be brief", "  /compact  "],
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
    from claude_dingtalk_bridge.claude_runner import MODEL_CHOICES

    names = [name for name, _ in MODEL_CHOICES]
    assert names == ["default", "opus", "sonnet", "haiku"]
