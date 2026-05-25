"""Test-wide fixtures.

log_context uses module-level contextvars (session id, turn, cwd, in-flight
tool-use map). Without isolation, an AssistantMessage test that records a
ToolUseBlock id→name mapping leaks into a later UserMessage test that asserts
on the id-only fallback. Reset between tests so each one sees a clean slate.
"""
import pytest

from claude_dingtalk_bridge import log_context


@pytest.fixture(autouse=True)
def _reset_log_context():
    log_context.clear()
    yield
    log_context.clear()
