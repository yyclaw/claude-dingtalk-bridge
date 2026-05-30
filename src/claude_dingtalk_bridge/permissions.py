"""Permission helpers wiring config.yaml rules into the Claude Agent SDK.

The bridge's config only carries a ``deny`` list. The flag-layer settings JSON
also auto-expands edit-shaped tools to the current project root so the phone
isn't asked for every in-project edit. The file is passed to the SDK via
``ClaudeAgentOptions(settings=...)`` at the highest user-controlled precedence,
so the deny list here overrides allow rules in lower settings layers
(``~/.claude/settings.json``, project ``.claude/settings.json``).

The Bash deny check lives in :mod:`.permission_hooks` as a PreToolUse hook so
it intercepts *before* any settings-layer allow short-circuit can fire.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_dingtalk_bridge.config import PermissionRules

_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def build_permission_settings(rules: PermissionRules, cwd: str) -> dict[str, Any]:
    """Build the flag-layer settings dict for the SDK to load.

    The shape matches ``~/.claude/settings.json``. Each edit-shaped tool is
    scoped to ``cwd`` via a ``Tool(<cwd>/**)`` allow pattern so in-project
    edits don't escalate to the phone.
    """
    allow = [f"{tool}({cwd}/**)" for tool in _EDIT_TOOLS]
    return {
        "permissions": {
            "allow": allow,
            "deny": list(rules.deny),
        }
    }


def write_permission_settings_file(
    rules: PermissionRules, cwd: str, path: Path
) -> Path:
    """Write the settings dict to ``path`` atomically; return the path.

    Atomic write avoids the SDK reading a half-written file if a turn racing
    with another turn ever triggers a regenerate.
    """
    settings = build_permission_settings(rules, cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2))
    tmp.replace(path)
    return path
