import pytest

from claude_dingtalk_bridge.config import PermissionRules
from claude_dingtalk_bridge.permissions import Decision, PermissionPolicy

PROJECT = "/tmp/proj"


def make_policy() -> PermissionPolicy:
    rules = PermissionRules(
        allowed_tools=["Read", "Glob", "Grep"],
        allowed_bash=["git status", "git diff", "ls", "go test"],
        allow_edits_in_project=True,
    )
    return PermissionPolicy(rules)


def test_readonly_tool_allowed():
    assert make_policy().evaluate("Read", {"file_path": "/etc/hosts"}, PROJECT) is Decision.ALLOW


def test_unknown_tool_escalates():
    assert make_policy().evaluate("WebFetch", {"url": "http://x"}, PROJECT) is Decision.ESCALATE


def test_edit_inside_project_allowed():
    decision = make_policy().evaluate("Edit", {"file_path": "/tmp/proj/src/a.py"}, PROJECT)
    assert decision is Decision.ALLOW


def test_write_inside_project_allowed():
    decision = make_policy().evaluate("Write", {"file_path": "/tmp/proj/new.py"}, PROJECT)
    assert decision is Decision.ALLOW


def test_edit_outside_project_escalates():
    decision = make_policy().evaluate("Edit", {"file_path": "/tmp/other/a.py"}, PROJECT)
    assert decision is Decision.ESCALATE


def test_edit_escaping_with_dotdot_escalates():
    decision = make_policy().evaluate("Edit", {"file_path": "/tmp/proj/../other/a.py"}, PROJECT)
    assert decision is Decision.ESCALATE


def test_edit_without_path_escalates():
    assert make_policy().evaluate("Edit", {}, PROJECT) is Decision.ESCALATE


def test_edit_escalates_when_disabled():
    rules = PermissionRules(allowed_tools=[], allowed_bash=[], allow_edits_in_project=False)
    policy = PermissionPolicy(rules)
    assert policy.evaluate("Edit", {"file_path": "/tmp/proj/a.py"}, PROJECT) is Decision.ESCALATE


def test_bash_whitelisted_prefix_allowed():
    assert make_policy().evaluate("Bash", {"command": "git status"}, PROJECT) is Decision.ALLOW
    assert make_policy().evaluate("Bash", {"command": "git diff HEAD"}, PROJECT) is Decision.ALLOW


def test_bash_non_whitelisted_escalates():
    assert make_policy().evaluate("Bash", {"command": "rm -rf /"}, PROJECT) is Decision.ESCALATE


def test_bash_prefix_must_be_word_boundary():
    assert make_policy().evaluate("Bash", {"command": "git statusx"}, PROJECT) is Decision.ESCALATE


def test_bash_with_shell_metachars_always_escalates():
    for cmd in [
        "git status && rm -rf /",
        "ls; rm x",
        "ls | sh",
        "git diff > /tmp/x",
        "git status `whoami`",
        "ls $(echo /)",
        "ls & rm -rf ~",
        "ls&rm",
        # Globs would turn `rm` (or any other prefix) into "delete everything
        # in cwd" — escalate so the user actually sees what's about to expand.
        "ls *.py",
        "ls foo?.txt",
        "ls {a,b}.txt",
        # ${VAR} expansion is just as dangerous as $(...) for a sneaky payload.
        "echo ${PATH}",
        # \r line splits would defeat a single-line allowlist check.
        "ls\rrm -rf /",
    ]:
        assert make_policy().evaluate("Bash", {"command": cmd}, PROJECT) is Decision.ESCALATE


def test_bash_empty_command_escalates():
    assert make_policy().evaluate("Bash", {"command": ""}, PROJECT) is Decision.ESCALATE


def test_edit_escalates_when_project_path_resolution_errors(monkeypatch):
    # A project_path that cannot be resolved (OSError/RuntimeError) must deny,
    # not crash.
    from claude_dingtalk_bridge import permissions

    def boom(self, *args, **kwargs):
        raise OSError("cannot resolve")

    monkeypatch.setattr(permissions.Path, "resolve", boom)
    assert make_policy().evaluate("Edit", {"file_path": "/x/y"}, PROJECT) is Decision.ESCALATE


def test_edit_escalates_when_target_path_resolution_errors(monkeypatch):
    # Resolve fine for project base, raise for the target file.
    from claude_dingtalk_bridge import permissions

    real_resolve = permissions.Path.resolve
    calls = {"n": 0}

    def boom_after_base(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_resolve(self, *args, **kwargs)
        raise RuntimeError("cannot resolve symlink loop")

    monkeypatch.setattr(permissions.Path, "resolve", boom_after_base)
    assert make_policy().evaluate("Edit", {"file_path": "/x/y"}, PROJECT) is Decision.ESCALATE


def test_edit_reuses_cached_project_resolve(monkeypatch):
    # Repeated edits with the same project_path must only resolve the base once.
    from claude_dingtalk_bridge import permissions

    real_resolve = permissions.Path.resolve
    base_calls = []

    def counting_resolve(self, *args, **kwargs):
        if str(self) == PROJECT:
            base_calls.append(str(self))
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(permissions.Path, "resolve", counting_resolve)
    policy = make_policy()
    policy.evaluate("Edit", {"file_path": "/tmp/proj/a.py"}, PROJECT)
    policy.evaluate("Edit", {"file_path": "/tmp/proj/b.py"}, PROJECT)
    assert len(base_calls) == 1
