from dataclasses import dataclass
from pathlib import Path

from claude_dingtalk_bridge.sessions import (
    collapse_inline_paths,
    display_path,
    format_relative_time,
    format_session_list,
    format_size,
    is_uuid,
    md_escape,
)


@dataclass
class FakeInfo:
    session_id: str
    summary: str
    last_modified: int
    git_branch: str | None = None
    file_size: int = 1234


def test_is_uuid_accepts_valid():
    assert is_uuid("550e8400-e29b-41d4-a716-446655440000")


def test_is_uuid_rejects_number_and_garbage():
    assert not is_uuid("3")
    assert not is_uuid("not-a-uuid")


def test_relative_time_buckets():
    now = 1_000_000_000_000
    assert format_relative_time(now, now) == "just now"
    assert format_relative_time(now - 120_000, now) == "2m ago"
    assert format_relative_time(now - 7_200_000, now) == "2h ago"
    assert format_relative_time(now - 86_400_000, now) == "yesterday"
    assert format_relative_time(now - 3 * 86_400_000, now) == "3d ago"


def test_md_escape_neutralizes_special_chars():
    assert md_escape("plain text") == "plain text"
    assert md_escape("a_b*c") == "a&#95;b&#42;c"
    assert md_escape("/resume <n>") == "/resume &#60;n&#62;"
    # '&' is escaped first, so emitted entities are not double-escaped.
    assert md_escape("a<b") == "a&#60;b"
    assert "&#38;#" not in md_escape("x*y")


def test_format_session_list_escapes_summary():
    infos = [FakeInfo("a" * 8, "fix _bug_ in <handler>", 0, "main")]
    text = format_session_list(infos, current_id=None)
    assert "<handler>" not in text
    assert "&#60;handler&#62;" in text
    assert "&#95;bug&#95;" in text


def test_format_size_buckets():
    assert format_size(0) == "0B"
    assert format_size(1023) == "1023B"
    assert format_size(38810) == "37.9KB"
    assert format_size(1258291) == "1.2MB"


def test_format_session_list_marks_current():
    infos = [
        FakeInfo("aaaaaaaa-0000-0000-0000-000000000000", "fix login", 0, "main"),
        FakeInfo("bbbbbbbb-0000-0000-0000-000000000000", "write docs", 0),
    ]
    text = format_session_list(infos, current_id=infos[1].session_id)
    assert "📋 **Recent sessions (2)**" in text
    assert "**1. fix login**" in text and "**2. write docs**" in text
    assert "main · " in text
    assert "⭐ current" in text
    assert "💬 `/resume <n>` to switch" in text


def test_format_session_list_branchless_entry():
    infos = [FakeInfo("aaaaaaaa-0000-0000-0000-000000000000", "no branch", 0)]
    text = format_session_list(infos, current_id=None)
    meta = next(line for line in text.splitlines() if "1.2KB" in line)
    assert meta.count("·") == 1  # time · size, no branch


def test_format_session_list_truncates_long_summary():
    infos = [FakeInfo("aaaaaaaa-0000-0000-0000-000000000000", "x" * 200, 0, "main")]
    text = format_session_list(infos, current_id=None)
    assert "…" in text
    assert "x" * 200 not in text


def test_display_path_collapses_home_to_tilde():
    inside = Path.home() / ".claude" / "projects" / "abc" / "x.jsonl"
    assert display_path(inside) == "~/.claude/projects/abc/x.jsonl"


def test_display_path_leaves_paths_outside_home_absolute():
    outside = Path("/tmp/elsewhere/file.jsonl")
    assert display_path(outside) == "/tmp/elsewhere/file.jsonl"


def test_display_path_project_relative_takes_precedence_over_home():
    # Project-relative is the more specific rewrite — when both could apply
    # (project root is under $HOME), the path should render relative to the
    # project, not as ~/proj/src/x.
    p = "/Users/dev/proj/src/x.py"
    assert display_path(p, cwd="/Users/dev/proj") == "src/x.py"


def test_display_path_returns_dot_when_path_equals_cwd():
    # Bare project root → "." rather than "" (which would render as an empty
    # field and lose all signal that a path was there).
    assert display_path("/Users/dev/proj", cwd="/Users/dev/proj") == "."


def test_display_path_outside_project_still_collapses_home():
    # Path inside $HOME but outside the project still gets the ~/ treatment.
    p = str(Path.home() / "notes" / "x.md")
    assert display_path(p, cwd="/Users/dev/proj") == "~/notes/x.md"


def test_collapse_inline_paths_rewrites_embedded_project_path():
    # Free-form string (Bash command) with an embedded absolute path inside
    # the project — the path collapses, the rest of the command is untouched.
    s = "grep -n foo /Users/dev/proj/src/a.py"
    assert collapse_inline_paths(s, cwd="/Users/dev/proj") == "grep -n foo src/a.py"


def test_collapse_inline_paths_falls_through_to_home_for_outside_paths():
    s = "cat " + str(Path.home() / "x.txt")
    assert collapse_inline_paths(s, cwd="/Users/dev/proj") == "cat ~/x.txt"


def test_collapse_inline_paths_noop_when_no_cwd_and_path_outside_home():
    assert collapse_inline_paths("ls /tmp/x", cwd="") == "ls /tmp/x"


def test_collapse_inline_paths_empty_string_unchanged():
    assert collapse_inline_paths("") == ""


import claude_dingtalk_bridge.sessions as sessions_mod


async def test_list_recent_sessions_passes_args(monkeypatch):
    captured = {}

    def fake_list_sessions(directory, limit):
        captured["directory"] = directory
        captured["limit"] = limit
        return ["s1", "s2"]

    monkeypatch.setattr(sessions_mod, "list_sessions", fake_list_sessions)
    result = await sessions_mod.list_recent_sessions("/tmp/proj", 7)
    assert result == ["s1", "s2"]
    assert captured == {"directory": "/tmp/proj", "limit": 7}


async def test_find_session_passes_args(monkeypatch):
    captured = {}

    def fake_get_session_info(session_id, directory):
        captured["session_id"] = session_id
        captured["directory"] = directory
        return None

    monkeypatch.setattr(sessions_mod, "get_session_info", fake_get_session_info)
    result = await sessions_mod.find_session("/tmp/proj", "the-id")
    assert result is None
    assert captured == {"session_id": "the-id", "directory": "/tmp/proj"}
