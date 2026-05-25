"""Test-wide fixtures.

log_context uses module-level contextvars (session id, turn, cwd, in-flight
tool-use map). Without isolation, an AssistantMessage test that records a
ToolUseBlock id→name mapping leaks into a later UserMessage test that asserts
on the id-only fallback. Reset between tests so each one sees a clean slate.
"""
from pathlib import Path

import pytest

from claude_dingtalk_bridge import log_context


@pytest.fixture(autouse=True)
def _reset_log_context():
    log_context.clear()
    yield
    log_context.clear()


@pytest.fixture
def write_config(tmp_path):
    """Write a config YAML to a tmp file with the perms load_config requires.

    `load_config` rejects looser-than-owner permissions, so tmp files created
    with the default umask (typically 0o644) would otherwise fail every load.
    Mirrors what `make config` does in production.
    """
    def _write(content: str) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(content)
        path.chmod(0o600)
        return path
    return _write
