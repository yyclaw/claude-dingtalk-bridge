from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "claude-dingtalk-bridge" / "config.yaml"


class ConfigError(Exception):
    """Raised when the config file is missing or invalid."""


@dataclass
class Project:
    name: str
    path: str


@dataclass
class PermissionRules:
    allowed_tools: list[str]
    allowed_bash: list[str]
    allow_edits_in_project: bool


@dataclass
class GeoConfig:
    proxy_url: str = "http://127.0.0.1:8118"
    target_country: str = "US"
    geo_service: str = "http://ip-api.com/json"
    timeout_seconds: int = 3


@dataclass
class Config:
    dingtalk_client_id: str
    dingtalk_client_secret: str
    authorized_user_id: str
    projects: list[Project]
    permissions: PermissionRules
    permission_timeout_seconds: int = 600
    geo: GeoConfig | None = None


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    try:
        dingtalk = raw["dingtalk"]
        projects = [
            Project(name=str(p["name"]), path=str(Path(p["path"]).expanduser()))
            for p in raw["projects"]
        ]
        perms_raw = raw.get("permissions", {}) or {}
        perms = PermissionRules(
            allowed_tools=list(perms_raw.get("allowed_tools", ["Read", "Glob", "Grep"])),
            allowed_bash=list(perms_raw.get("allowed_bash", [])),
            allow_edits_in_project=bool(perms_raw.get("allow_edits_in_project", True)),
        )
        geo = None
        if "geo" in raw:
            geo_raw = raw["geo"] or {}
            geo = GeoConfig(
                proxy_url=str(geo_raw.get("proxy_url", "http://127.0.0.1:8118")),
                target_country=str(geo_raw.get("target_country", "US")),
                geo_service=str(geo_raw.get("geo_service", "http://ip-api.com/json")),
                timeout_seconds=int(geo_raw.get("timeout_seconds", 3)),
            )
        config = Config(
            dingtalk_client_id=str(dingtalk["client_id"]),
            dingtalk_client_secret=str(dingtalk["client_secret"]),
            authorized_user_id=str(raw["authorized_user_id"]),
            projects=projects,
            permissions=perms,
            permission_timeout_seconds=int(raw.get("permission_timeout_seconds", 600)),
            geo=geo,
        )
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"Invalid config: {exc}") from exc
    if not config.projects:
        raise ConfigError("At least one project must be configured")
    return config
