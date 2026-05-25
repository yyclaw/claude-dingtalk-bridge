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


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_load_valid_config(tmp_path):
    config = load_config(_write(tmp_path, VALID))
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


def test_missing_required_key_raises(tmp_path):
    bad = "dingtalk:\n  client_id: x\n  client_secret: y\nprojects: []\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad))


def test_empty_projects_raises(tmp_path):
    bad = """
dingtalk:
  client_id: x
  client_secret: y
authorized_user_id: z
projects: []
"""
    with pytest.raises(ConfigError, match="project"):
        load_config(_write(tmp_path, bad))


def test_defaults_applied(tmp_path):
    minimal = """
dingtalk:
  client_id: x
  client_secret: y
authorized_user_id: z
projects:
  - name: p
    path: /tmp/p
"""
    config = load_config(_write(tmp_path, minimal))
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


def test_geo_section_parsed(tmp_path):
    config = load_config(_write(tmp_path, GEO))
    assert isinstance(config.geo, GeoConfig)
    assert config.geo.proxy_url == "http://127.0.0.1:9999"
    assert config.geo.target_country == "JP"
    assert config.geo.geo_service == "http://example.com/json"
    assert config.geo.timeout_seconds == 5


def test_geo_absent_is_none(tmp_path):
    config = load_config(_write(tmp_path, VALID))
    assert config.geo is None


def test_geo_empty_uses_defaults(tmp_path):
    text = VALID + "geo: {}\n"
    config = load_config(_write(tmp_path, text))
    assert isinstance(config.geo, GeoConfig)
    assert config.geo.proxy_url == "http://127.0.0.1:8118"
    assert config.geo.target_country == "US"
    assert config.geo.geo_service == "http://ip-api.com/json"
    assert config.geo.timeout_seconds == 3
