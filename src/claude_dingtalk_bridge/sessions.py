from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from claude_agent_sdk import (
    get_session_info,
    list_sessions,
    project_key_for_directory,
)

from claude_dingtalk_bridge.display import (
    format_relative_time,
    format_size,
    md_escape,
)

_SUMMARY_LIMIT = 60


def is_uuid(text: str) -> bool:
    """True when text is a canonical UUID string."""
    try:
        return str(uuid.UUID(text)) == text.lower()
    except ValueError:
        return False


def format_session_list(infos, current_id: str | None) -> str:
    """Render a numbered, TUI-styled markdown list of recent sessions.

    Each entry is a bold title (the summary) followed by a metadata line
    `time · branch · size`, mirroring the desktop TUI. The leading index
    is kept so the phone can reply with `/resume <n>`.
    """
    blocks = [f"📋 **Recent sessions ({len(infos)})**"]
    for idx, info in enumerate(infos, start=1):
        summary = (info.summary or "(no summary)").replace("\n", " ").strip()
        if len(summary) > _SUMMARY_LIMIT:
            summary = summary[:_SUMMARY_LIMIT] + "…"
        summary = md_escape(summary)
        meta = ["　" + format_relative_time(info.last_modified)]
        if info.git_branch:
            meta.append(md_escape(info.git_branch))
        meta.append(format_size(info.file_size))
        if info.session_id == current_id:
            meta.append("⭐ current")
        blocks.append(f"**{idx}. {summary}**  \n{' · '.join(meta)}")
    blocks.append("💬 `/resume <n>` to switch")
    return "\n\n".join(blocks)


def session_transcript_path(project_path: str, session_id: str) -> Path:
    """Filesystem path of the SDK-maintained JSONL transcript for a session.

    Mirrors the CLI/SDK convention: ``~/.claude/projects/<project-key>/<sid>.jsonl``.
    The path is computed, not stat'd — call sites can present it to the user
    even before the file exists.
    """
    key = project_key_for_directory(project_path)
    return Path.home() / ".claude" / "projects" / key / f"{session_id}.jsonl"


async def list_recent_sessions(project_path: str, limit: int):
    """Recent sessions for a project, newest first (SDK file-IO off-thread)."""
    return await asyncio.to_thread(
        list_sessions, directory=project_path, limit=limit
    )


async def find_session(project_path: str, session_id: str):
    """Look up one session by id within a project; None if not found."""
    return await asyncio.to_thread(
        get_session_info, session_id, directory=project_path
    )
