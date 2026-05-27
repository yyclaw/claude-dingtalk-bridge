from __future__ import annotations

from enum import Enum, auto
from pathlib import Path

from claude_dingtalk_bridge.config import PermissionRules


class Decision(Enum):
    ALLOW = auto()
    ESCALATE = auto()


_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Any one of these in the command escalates: shell would interpret it as
# redirection, glob expansion, brace expansion, command substitution or
# command chaining. Substring match is intentional — even inside quotes
# these turn a "prefix + args" allowlist into arbitrary code. Glob chars
# (`*` `?` `{`) are listed because `rm *` under an `rm` prefix would
# auto-delete the cwd.
_SHELL_METACHARS = (
    "&&", "||", "&", ";", "|", ">", "<", "`", "$(", "${",
    "\n", "\r", "*", "?", "{",
)


class PermissionPolicy:
    def __init__(self, rules: PermissionRules):
        self._rules = rules
        # Single-slot cache; project_path is stable within a project and only
        # changes on /cd, so caching avoids a redundant resolve() per edit.
        self._project_cache: tuple[str, Path] | None = None

    def evaluate(self, tool_name: str, tool_input: dict, project_path: str) -> Decision:
        if tool_name in self._rules.allowed_tools:
            return Decision.ALLOW
        if tool_name in _EDIT_TOOLS:
            return self._evaluate_edit(tool_input, project_path)
        if tool_name == "Bash":
            return self._evaluate_bash(tool_input)
        return Decision.ESCALATE

    def _resolve_project(self, project_path: str) -> Path | None:
        cache = self._project_cache
        if cache is not None and cache[0] == project_path:
            return cache[1]
        try:
            resolved = Path(project_path).expanduser().resolve()
        except (OSError, RuntimeError):
            return None
        self._project_cache = (project_path, resolved)
        return resolved

    def _evaluate_edit(self, tool_input: dict, project_path: str) -> Decision:
        if not self._rules.allow_edits_in_project:
            return Decision.ESCALATE
        target = tool_input.get("file_path") or tool_input.get("path")
        if not target:
            return Decision.ESCALATE
        base = self._resolve_project(project_path)
        if base is None:
            return Decision.ESCALATE
        try:
            target_resolved = Path(target).expanduser().resolve()
        except (OSError, RuntimeError):
            return Decision.ESCALATE
        return Decision.ALLOW if target_resolved.is_relative_to(base) else Decision.ESCALATE

    def _evaluate_bash(self, tool_input: dict) -> Decision:
        command = (tool_input.get("command") or "").strip()
        if not command:
            return Decision.ESCALATE
        if any(meta in command for meta in _SHELL_METACHARS):
            return Decision.ESCALATE
        for prefix in self._rules.allowed_bash:
            if command == prefix or command.startswith(prefix + " "):
                return Decision.ALLOW
        return Decision.ESCALATE
