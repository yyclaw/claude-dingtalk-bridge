import pytest

from claude_dingtalk_bridge.config import Project
from claude_dingtalk_bridge.projects import ProjectRegistry

PROJECTS = [
    Project(name="multica", path="/tmp/multica"),
    Project(name="docs", path="/tmp/docs"),
]


def test_names_in_order():
    registry = ProjectRegistry(PROJECTS)
    assert registry.names() == ["multica", "docs"]


def test_get_by_name():
    registry = ProjectRegistry(PROJECTS)
    assert registry.get("docs").path == "/tmp/docs"


def test_get_unknown_returns_none():
    registry = ProjectRegistry(PROJECTS)
    assert registry.get("nope") is None


def test_default_is_first():
    registry = ProjectRegistry(PROJECTS)
    assert registry.default().name == "multica"


def test_empty_projects_raises():
    with pytest.raises(ValueError):
        ProjectRegistry([])


def test_duplicate_names_rejected():
    # Duplicate names would collapse silently in lookups while still listing
    # twice; the registry refuses them so the ambiguity surfaces at startup.
    first = Project(name="shared", path="/tmp/first")
    last = Project(name="shared", path="/tmp/last")
    with pytest.raises(ValueError, match="shared"):
        ProjectRegistry([first, last])
