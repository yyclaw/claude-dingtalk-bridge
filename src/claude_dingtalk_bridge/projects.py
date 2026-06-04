from __future__ import annotations

from claude_dingtalk_bridge.config import Project


class ProjectRegistry:
    def __init__(self, projects: list[Project]):
        if not projects:
            raise ValueError("projects must not be empty")
        names = [p.name for p in projects]
        # Duplicate names would silently collapse in _by_name (last wins) while
        # names()/`/ls` still showed both — `/cd <name>` would then reach an
        # entry the listing never disambiguated. Reject up front.
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate project names: {', '.join(dupes)}")
        self._projects = list(projects)
        self._by_name = {p.name: p for p in projects}

    def names(self) -> list[str]:
        return [p.name for p in self._projects]

    def get(self, name: str) -> Project | None:
        return self._by_name.get(name)

    def default(self) -> Project:
        return self._projects[0]
