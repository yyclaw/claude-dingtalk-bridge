import json

from claude_dingtalk_bridge.config import PermissionRules
from claude_dingtalk_bridge.permissions import (
    build_permission_settings,
    write_permission_settings_file,
)


def _rules(**overrides) -> PermissionRules:
    base = dict(deny=[])
    base.update(overrides)
    return PermissionRules(**base)


def test_build_returns_settings_json_shape():
    out = build_permission_settings(_rules(), "/tmp/p")
    assert set(out.keys()) == {"permissions"}
    assert set(out["permissions"].keys()) == {"allow", "deny"}


def test_deny_passthrough():
    out = build_permission_settings(_rules(deny=["Bash(rm -rf:*)"]), "/tmp/p")
    assert out["permissions"]["deny"] == ["Bash(rm -rf:*)"]


def test_in_project_edits_always_appended():
    # All four edit-shaped tools must be present so MultiEdit / NotebookEdit
    # don't slip through with only Edit/Write covered.
    out = build_permission_settings(_rules(), "/tmp/proj")
    allow = out["permissions"]["allow"]
    assert "Edit(/tmp/proj/**)" in allow
    assert "Write(/tmp/proj/**)" in allow
    assert "MultiEdit(/tmp/proj/**)" in allow
    assert "NotebookEdit(/tmp/proj/**)" in allow


def test_cwd_normalised_into_glob(tmp_path):
    out = build_permission_settings(_rules(), str(tmp_path))
    expected = f"Edit({tmp_path}/**)"
    assert expected in out["permissions"]["allow"]


def test_write_creates_file_with_expected_json(tmp_path):
    path = tmp_path / "perms.json"
    written = write_permission_settings_file(
        _rules(deny=["Bash(rm:*)"]), "/tmp/p", path
    )
    assert written == path
    payload = json.loads(path.read_text())
    assert payload["permissions"]["deny"] == ["Bash(rm:*)"]
    assert payload["permissions"]["allow"] == [
        "Edit(/tmp/p/**)",
        "Write(/tmp/p/**)",
        "MultiEdit(/tmp/p/**)",
        "NotebookEdit(/tmp/p/**)",
    ]


def test_write_is_atomic_overwrite(tmp_path):
    path = tmp_path / "perms.json"
    write_permission_settings_file(_rules(deny=["Bash(rm:*)"]), "/tmp/p", path)
    write_permission_settings_file(_rules(deny=["Bash(curl:*)"]), "/tmp/p", path)
    payload = json.loads(path.read_text())
    assert payload["permissions"]["deny"] == ["Bash(curl:*)"]


def test_write_creates_parent_dirs(tmp_path):
    nested = tmp_path / "a" / "b" / "perms.json"
    write_permission_settings_file(_rules(), "/tmp/p", nested)
    assert nested.exists()
