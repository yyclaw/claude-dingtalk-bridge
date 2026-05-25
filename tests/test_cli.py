from unittest.mock import patch

import pytest

from claude_dingtalk_bridge import cli, launchd
from claude_dingtalk_bridge.cli import main


def test_agent_plist_content_runs_daemon_via_helper():
    content = launchd.agent_plist_content()
    assert f"<string>{launchd.LABEL}</string>" in content
    assert f"<string>{launchd._helper_path()}</string>" in content
    assert "<string>run</string>" in content
    assert "<string>claude_dingtalk_bridge</string>" in content
    assert "<key>RunAtLoad</key>" in content
    assert "<key>KeepAlive</key>" in content


def test_app_info_plist_has_friendly_name():
    content = launchd.app_info_plist()
    assert f"<key>CFBundleName</key><string>{launchd.APP_NAME}</string>" in content
    assert f"<key>CFBundleIdentifier</key><string>{launchd.LABEL}</string>" in content
    assert f"<key>CFBundleExecutable</key><string>{launchd._HELPER_NAME}</string>" in content
    assert "CFBundleIconFile" not in content


def test_app_info_plist_includes_icon_when_given():
    assert "<key>CFBundleIconFile</key><string>icon</string>" in launchd.app_info_plist("icon")


def test_cli_install_invokes_launchd_install():
    with patch.object(launchd, "install") as install:
        assert main(["install"]) == 0
        install.assert_called_once()


def test_cli_stop_invokes_launchd_stop():
    with patch.object(launchd, "stop") as stop, \
         patch.object(cli, "_notify_phone") as notify:
        assert main(["stop"]) == 0
        stop.assert_called_once()
        notify.assert_called_once_with("🛑 Daemon stopped.")


def test_cli_status_prints_status():
    with patch.object(launchd, "status", return_value="running"):
        assert main(["status"]) == 0


def test_cli_uninstall_invokes_launchd_uninstall():
    with patch.object(launchd, "uninstall") as uninstall:
        assert main(["uninstall"]) == 0
        uninstall.assert_called_once()


def test_cli_start_reports_friendly_error_when_not_installed(capsys):
    with patch.object(launchd, "start", side_effect=launchd.LaunchdError("not installed")):
        assert main(["start"]) == 1
    assert "not installed" in capsys.readouterr().err


def test_start_raises_launchd_error_when_not_installed(tmp_path):
    missing = tmp_path / "missing.plist"
    with patch.object(launchd, "_is_loaded", return_value=False), \
         patch.object(launchd, "_agent_plist_path", return_value=missing):
        with pytest.raises(launchd.LaunchdError):
            launchd.start()


def test_stop_raises_launchd_error_when_service_not_loaded():
    with patch.object(launchd, "_is_loaded", return_value=False):
        with pytest.raises(launchd.LaunchdError):
            launchd.stop()


def test_cli_stop_reports_friendly_error_when_not_installed(capsys):
    with patch.object(launchd, "stop", side_effect=launchd.LaunchdError("nothing to stop")):
        assert main(["stop"]) == 1
    assert "nothing to stop" in capsys.readouterr().err


def test_cli_install_reports_approval_needed(capsys):
    with patch.object(launchd, "install", return_value="requiresApproval"):
        assert main(["install"]) == 0
    assert "approval" in capsys.readouterr().out


def test_cli_install_reports_registered(capsys):
    with patch.object(launchd, "install", return_value="enabled"):
        assert main(["install"]) == 0
    assert "registered (enabled)" in capsys.readouterr().out


def test_cli_start_invokes_launchd_start(capsys):
    with patch.object(launchd, "start") as start, \
         patch.object(cli, "_notify_phone") as notify:
        assert main(["start"]) == 0
        start.assert_called_once()
        notify.assert_called_once_with("✅ Daemon started.")
    assert "Started." in capsys.readouterr().out


def test_cli_restart_invokes_launchd_restart(capsys):
    with patch.object(launchd, "restart") as restart, \
         patch.object(cli, "_notify_phone") as notify:
        assert main(["restart"]) == 0
        restart.assert_called_once()
        notify.assert_called_once_with("🔄 Daemon restarted.")
    assert "Restarted." in capsys.readouterr().out


def test_cli_lifecycle_skips_notification_when_launchd_fails():
    # If launchd raises, the CLI bails out and no phone notice should fire --
    # we don't want to lie to the user that the daemon restarted.
    with patch.object(launchd, "restart", side_effect=launchd.LaunchdError("nope")), \
         patch.object(cli, "_notify_phone") as notify:
        assert main(["restart"]) == 1
        notify.assert_not_called()


def test_cli_status_does_not_notify():
    # status / install / uninstall don't change daemon lifecycle from the
    # phone's perspective, so they must not push a notification.
    with patch.object(launchd, "status", return_value="running"), \
         patch.object(cli, "_notify_phone") as notify:
        assert main(["status"]) == 0
        notify.assert_not_called()


def test_notify_phone_uses_transport_with_config(monkeypatch):
    sent: list = []
    fake_config = type(
        "C", (), {
            "dingtalk_client_id": "id",
            "dingtalk_client_secret": "secret",
            "authorized_user_id": "staff-1",
        },
    )()

    class _FakeTransport:
        def __init__(self, client_id, client_secret):
            sent.append(("init", client_id, client_secret))

        def send_text(self, user_id, text):
            sent.append(("send", user_id, text))

    monkeypatch.setattr(
        "claude_dingtalk_bridge.config.load_config", lambda: fake_config
    )
    monkeypatch.setattr(
        "claude_dingtalk_bridge.dingtalk.DingTalkTransport", _FakeTransport
    )
    cli._notify_phone("hello")
    assert sent == [("init", "id", "secret"), ("send", "staff-1", "hello")]


def test_notify_phone_reports_failure_to_stderr_without_raising(capsys, monkeypatch):
    def _boom():
        raise RuntimeError("config gone")

    monkeypatch.setattr("claude_dingtalk_bridge.config.load_config", _boom)
    # Must not raise -- the launchd action already succeeded.
    cli._notify_phone("hello")
    err = capsys.readouterr().err
    assert "could not send phone notice" in err
    assert "config gone" in err


def test_cli_run_as_main_module(monkeypatch):
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["claude-dingtalk-bridge", "status"])
    # Drop the already-imported module so runpy executes it cleanly rather
    # than re-running one still cached in sys.modules.
    monkeypatch.delitem(sys.modules, "claude_dingtalk_bridge.cli", raising=False)
    with patch.object(launchd, "status", return_value="running"):
        with pytest.raises(SystemExit) as exc:
            runpy.run_module("claude_dingtalk_bridge.cli", run_name="__main__")
    assert exc.value.code == 0
