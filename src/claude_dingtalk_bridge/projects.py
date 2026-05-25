from __future__ import annotations

from claude_dingtalk_bridge.config import Project


class ProjectRegistry:
    def __init__(self, projects: list[Project]):
        if not projects:
            raise ValueError("projects must not be empty")
        self._projects = list(projects)
        self._by_name = {p.name: p for p in projects}

    def names(self) -> list[str]:
        return [p.name for p in self._projects]

    def get(self, name: str) -> Project | None:
        return self._by_name.get(name)

    def default(self) -> Project:
        return self._projects[0]
