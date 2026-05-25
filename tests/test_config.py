from pathlib import Path

import pytest

from claude_dingtalk_bridge.config import Config, ConfigError, GeoConfig, load_config

VALID = """
dingtalk:
  client_id: appkey123
  client_secret: secret456
authorized_user_id: staff789
projects:
  - name: multica
    path: ~/Projects/marmot-multica
permissions:
  allow_edits_in_project: true
  allowed_tools: [Read, Grep]
  allowed_bash: ["git status"]
permission_timeout_seconds: 300
"""


def test_load_valid_config(write_config):
    config = load_config(write_config(VALID))
    assert isinstance(config, Config)
    assert config.dingtalk_client_id == "appkey123"
    assert config.dingtalk_client_secret == "secret456"
    assert config.authorized_user_id == "staff789"
    assert config.permission_timeout_seconds == 300
    assert len(config.projects) == 1
    assert config.projects[0].name == "multica"
    assert config.projects[0].path == str(Path("~/Projects/marmot-multica").expanduser())
    assert config.permissions.allowed_tools == ["Read", "Grep"]
    assert config.permissions.allowed_bash == ["git status"]
    assert config.permissions.allow_edits_in_project is True


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_missing_required_key_raises(write_config):
    bad = "dingtalk:\n  client_id: x\n  client_secret: y\nprojects: []\n"
    with pytest.raises(ConfigError):
        load_config(write_config(bad))


def test_empty_projects_raises(write_config):
    bad = """
dingtalk:
  client_id: x
  client_secret: y
authorized_user_id: z
projects: []
"""
    with pytest.raises(ConfigError, match="project"):
        load_config(write_config(bad))


def test_defaults_applied(write_config):
    minimal = """
dingtalk:
  client_id: x
  client_secret: y
authorized_user_id: z
projects:
  - name: p
    path: /tmp/p
"""
    config = load_config(write_config(minimal))
    assert config.permission_timeout_seconds == 600
    assert config.permissions.allowed_tools == ["Read", "Glob", "Grep"]
    assert config.permissions.allow_edits_in_project is True


GEO = """
dingtalk:
  client_id: x
  client_secret: y
authorized_user_id: z
projects:
  - name: p
    path: /tmp/p
geo:
  proxy_url: http://127.0.0.1:9999
  target_country: JP
  geo_service: http://example.com/json
  timeout_seconds: 5
"""


def test_geo_section_parsed(write_config):
    config = load_config(write_config(GEO))
    assert isinstance(config.geo, GeoConfig)
    assert config.geo.proxy_url == "http://127.0.0.1:9999"
    assert config.geo.target_country == "JP"
    assert config.geo.geo_service == "http://example.com/json"
    assert config.geo.timeout_seconds == 5


def test_geo_absent_is_none(write_config):
    config = load_config(write_config(VALID))
    assert config.geo is None


def test_geo_empty_uses_defaults(write_config):
    text = VALID + "geo: {}\n"
    config = load_config(write_config(text))
    assert isinstance(config.geo, GeoConfig)
    assert config.geo.proxy_url == "http://127.0.0.1:8118"
    assert config.geo.target_country == "US"
    assert config.geo.geo_service == "http://ip-api.com/json"
    assert config.geo.timeout_seconds == 3


def test_load_config_rejects_world_readable(tmp_path):
    """A 0644 config exposes the client_secret to every local user — reject
    it with a fix-it message rather than load silently."""
    path = tmp_path / "config.yaml"
    path.write_text(VALID)
    path.chmod(0o644)
    with pytest.raises(ConfigError) as exc_info:
        load_config(path)
    msg = str(exc_info.value)
    assert "too permissive" in msg
    assert "0644" in msg
    assert "chmod 600" in msg


def test_load_config_rejects_group_readable(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID)
    path.chmod(0o640)
    with pytest.raises(ConfigError, match="too permissive"):
        load_config(path)
