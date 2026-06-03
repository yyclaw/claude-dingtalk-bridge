from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "claude-dingtalk-bridge" / "config.yaml"
CACHE_DIR = Path.home() / "Library" / "Caches" / "claude-dingtalk-bridge"


class ConfigError(Exception):
    """Raised when the config file is missing or invalid."""


@dataclass
class Project:
    name: str
    path: str


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
    permission_ask_timeout: int = 600
    geo: GeoConfig | None = None


def _parse_projects(raw: dict) -> list[Project]:
    try:
        projects = [
            Project(name=str(p["name"]), path=str(Path(p["path"]).expanduser()))
            for p in raw["projects"]
        ]
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"Invalid config: {exc}") from exc
    if not projects:
        raise ConfigError("At least one project must be configured")
    return projects


def load_projects(path: Path | str = DEFAULT_CONFIG_PATH) -> list[Project]:
    """Read just the `projects` section from the config file.

    Used by `/ls reload` to pick up edited projects without a restart. It
    deliberately skips the secret/perm validation `load_config` does — a reload
    re-reads only the project list, leaving the live dingtalk/geo config and
    session state untouched.
    """
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    return _parse_projects(raw)


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    # The config holds the DingTalk client_secret; anyone who can read it
    # can send messages as the bot for ~2 hours. Refuse looser-than-owner
    # perms, same bar ssh sets for private keys. `make config` already
    # chmods 600, so this fires only when the file was created by hand.
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ConfigError(
            f"Config file is too permissive (mode {mode:04o}): {path}\n"
            f"Run `chmod 600 \"{path}\"` to restrict it to the owner."
        )
    raw = yaml.safe_load(path.read_text()) or {}
    projects = _parse_projects(raw)
    try:
        dingtalk = raw["dingtalk"]
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
            permission_ask_timeout=int(raw.get("permission_ask_timeout", 600)),
            geo=geo,
        )
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"Invalid config: {exc}") from exc
    return config
