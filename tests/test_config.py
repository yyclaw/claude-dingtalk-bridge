from pathlib import Path

import pytest

from claude_dingtalk_bridge.config import (
    Config,
    ConfigError,
    GeoConfig,
    load_config,
    load_projects,
)

VALID = """
dingtalk:
  client_id: appkey123
  client_secret: secret456
authorized_user_id: staff789
projects:
  - name: multica
    path: ~/Projects/marmot-multica
permission_ask_timeout: 300
"""


def test_load_valid_config(write_config):
    config = load_config(write_config(VALID))
    assert isinstance(config, Config)
    assert config.dingtalk_client_id == "appkey123"
    assert config.dingtalk_client_secret == "secret456"
    assert config.authorized_user_id == "staff789"
    assert config.permission_ask_timeout == 300
    assert len(config.projects) == 1
    assert config.projects[0].name == "multica"
    assert config.projects[0].path == str(Path("~/Projects/marmot-multica").expanduser())


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


def test_duplicate_project_names_raise(write_config):
    bad = """
dingtalk:
  client_id: x
  client_secret: y
authorized_user_id: z
projects:
  - name: dup
    path: /tmp/a
  - name: dup
    path: /tmp/b
"""
    with pytest.raises(ConfigError, match="Duplicate project names: dup"):
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
    assert config.permission_ask_timeout == 600


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
  country_field: countryCode
  ip_field: query
"""


def test_geo_section_parsed(write_config):
    config = load_config(write_config(GEO))
    assert isinstance(config.geo, GeoConfig)
    assert config.geo.proxy_url == "http://127.0.0.1:9999"
    assert config.geo.target_country == "JP"
    assert config.geo.geo_service == "http://example.com/json"
    assert config.geo.timeout_seconds == 5
    assert config.geo.country_field == "countryCode"
    assert config.geo.ip_field == "query"


def test_geo_absent_is_none(write_config):
    config = load_config(write_config(VALID))
    assert config.geo is None


def test_geo_empty_uses_defaults(write_config):
    text = VALID + "geo: {}\n"
    config = load_config(write_config(text))
    assert isinstance(config.geo, GeoConfig)
    assert config.geo.proxy_url == "http://127.0.0.1:8118"
    assert config.geo.target_country == "US"
    assert config.geo.geo_service == "https://ipinfo.io/json"
    assert config.geo.timeout_seconds == 5
    assert config.geo.country_field == "country"
    assert config.geo.ip_field == "ip"


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


def test_load_config_missing_authorized_user_id_raises(write_config):
    """Projects parse cleanly but the dingtalk/authorized_user_id block is
    incomplete — load_config's outer except must wrap that as ConfigError."""
    bad = """
dingtalk:
  client_id: x
  client_secret: y
projects:
  - name: p
    path: /tmp/p
"""
    with pytest.raises(ConfigError, match="Invalid config"):
        load_config(write_config(bad))


def test_load_projects_returns_project_list(write_config):
    projects = load_projects(write_config(VALID))
    assert len(projects) == 1
    assert projects[0].name == "multica"
    assert projects[0].path == str(Path("~/Projects/marmot-multica").expanduser())


def test_load_projects_empty_raises(write_config):
    bad = """
dingtalk:
  client_id: x
  client_secret: y
authorized_user_id: z
projects: []
"""
    with pytest.raises(ConfigError, match="project"):
        load_projects(write_config(bad))


def test_load_projects_missing_key_raises(write_config):
    bad = "dingtalk:\n  client_id: x\n  client_secret: y\n"
    with pytest.raises(ConfigError):
        load_projects(write_config(bad))


def test_load_projects_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_projects(tmp_path / "nope.yaml")


# --- Gap 1: int() raises ValueError on non-integer strings ---

def test_non_integer_permission_ask_timeout_raises(write_config):
    # int("not-a-number") would escape as ValueError without the fix at config.py:107
    bad = VALID.replace("permission_ask_timeout: 300", "permission_ask_timeout: not-a-number")
    with pytest.raises(ConfigError, match="Invalid config"):
        load_config(write_config(bad))


def test_non_integer_geo_timeout_seconds_raises(write_config):
    text = VALID + "geo:\n  timeout_seconds: bad\n"
    with pytest.raises(ConfigError, match="Invalid config"):
        load_config(write_config(text))


# --- Gap 2: other-execute bit (0o001) must be caught by the 0o077 mask ---

def test_load_config_rejects_other_execute(tmp_path):
    # 0o601 & 0o077 == 0o001, so the mask catches it; guards against mask
    # narrowing mutations such as 0o070 that would miss the other-execute bit
    path = tmp_path / "config.yaml"
    path.write_text(VALID)
    path.chmod(0o601)
    with pytest.raises(ConfigError, match="too permissive"):
        load_config(path)


# --- Gap 3: a blank `geo:` key (null value) disables geo, like omitting it ---

def test_geo_null_value_disables_geo(write_config):
    # A half-written `geo:` section (null value) must not silently enable the
    # default proxy; only an explicit mapping (`geo: {}` or with content) opts
    # in. See test_geo_empty_uses_defaults for the explicit-empty case.
    config = load_config(write_config(VALID + "geo:\n"))
    assert config.geo is None


def test_load_projects_ignores_other_sections(write_config):
    """Reload reads only the projects list — a config whose other sections are
    absent or broken (here: no dingtalk/authorized_user_id) still yields the
    projects, since /ls reload must not depend on the rest being re-validated."""
    only_projects = """
projects:
  - name: solo
    path: /tmp/solo
"""
    projects = load_projects(write_config(only_projects))
    assert [p.name for p in projects] == ["solo"]
