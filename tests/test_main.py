import runpy
from unittest.mock import patch


def test_main_module_invokes_daemon_run():
    # Running the package as `python -m claude_dingtalk_bridge` must call run().
    with patch("claude_dingtalk_bridge.daemon.run") as run:
        runpy.run_module("claude_dingtalk_bridge", run_name="__main__")
    run.assert_called_once_with()


def test_main_module_plain_import_skips_run():
    # Imported as a normal module (__name__ != "__main__"), the run() guard
    # is skipped — importing it must not start the daemon.
    import claude_dingtalk_bridge.__main__  # noqa: F401
