from dataclasses import dataclass
from pathlib import Path

from claude_dingtalk_bridge.sessions import (
    format_session_list,
    is_uuid,
    project_key_for_directory,
    session_transcript_path,
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


def test_format_session_list_escapes_summary():
    infos = [FakeInfo("a" * 8, "fix _bug_ in <handler>", 0, "main")]
    text = format_session_list(infos, current_id=None)
    assert "<handler>" not in text
    assert "&#60;handler&#62;" in text
    assert "&#95;bug&#95;" in text


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


def test_format_session_list_current_marker_on_correct_entry():
    # Two entries; only entry 2 is current. Splitting on '\n\n' gives per-entry
    # blocks, letting us assert which numbered block carries '⭐ current'.
    # A mutation flipping == to != in sessions.py:48 would put the marker on
    # entry 1 and fail this test even though '⭐ current' is still in the full text.
    infos = [
        FakeInfo("aaaaaaaa-0000-0000-0000-000000000000", "fix login", 0, "main"),
        FakeInfo("bbbbbbbb-0000-0000-0000-000000000000", "write docs", 0),
    ]
    text = format_session_list(infos, current_id=infos[1].session_id)
    blocks = text.split("\n\n")
    entry1 = next(b for b in blocks if "**1." in b)
    entry2 = next(b for b in blocks if "**2." in b)
    assert "⭐ current" not in entry1
    assert "⭐ current" in entry2


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


def test_session_transcript_path_follows_sdk_convention():
    # Path is computed from the project key, not stat'd — so it's well-defined
    # even before the JSONL file exists.
    key = project_key_for_directory("/Users/dev/proj")
    expected = Path.home() / ".claude" / "projects" / key / "abc123.jsonl"
    assert session_transcript_path("/Users/dev/proj", "abc123") == expected
