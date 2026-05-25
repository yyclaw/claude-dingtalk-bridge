import subprocess
from unittest.mock import patch

import pytest

from claude_dingtalk_bridge import launchd


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


# --- low-level helpers --------------------------------------------------


def test_run_forwards_args_to_subprocess():
    with patch.object(launchd.subprocess, "run", return_value=_proc()) as run:
        launchd._run(["echo", "hi"], capture=True)
    args, kwargs = run.call_args
    assert args[0] == ["echo", "hi"]
    assert kwargs["capture_output"] is True


def test_service_target_includes_uid_and_label():
    target = launchd._service_target()
    assert target.startswith("gui/")
    assert target.endswith(launchd.LABEL)


def test_agent_plist_path_lives_inside_the_bundle():
    path = launchd._agent_plist_path()
    assert path.name == launchd._PLIST_NAME
    assert "LaunchAgents" in path.parts


# --- _xcrun -------------------------------------------------------------


def test_xcrun_returns_completed_process():
    with patch.object(launchd, "_run", return_value=_proc(stdout="ok")) as run:
        result = launchd._xcrun(["swiftc"], "compile")
    assert result.stdout == "ok"
    run.assert_called_once()


def test_xcrun_raises_friendly_error_when_tools_missing():
    with patch.object(launchd, "_run", side_effect=FileNotFoundError):
        with pytest.raises(launchd.LaunchdError, match="command line tools"):
            launchd._xcrun(["swiftc"], "compile the launch helper")


# --- _build_icns --------------------------------------------------------


def test_build_icns_resizes_and_packs(tmp_path):
    with patch.object(launchd, "_run", return_value=_proc()) as run:
        launchd._build_icns(tmp_path / "icon.icns")
    # one sips call per iconset size, plus the iconutil pack
    assert run.call_count == len(launchd._ICONSET_SIZES) + 1


def test_build_icns_raises_when_resize_fails(tmp_path):
    with patch.object(launchd, "_run", return_value=_proc(returncode=1, stderr="boom")):
        with pytest.raises(launchd.LaunchdError, match="resize the app icon"):
            launchd._build_icns(tmp_path / "icon.icns")


def test_build_icns_raises_when_pack_fails(tmp_path):
    # sips succeeds for every size, iconutil (the final call) fails.
    outcomes = [_proc()] * len(launchd._ICONSET_SIZES) + [_proc(returncode=1, stdout="bad")]
    with patch.object(launchd, "_run", side_effect=outcomes):
        with pytest.raises(launchd.LaunchdError, match="build the app icon"):
            launchd._build_icns(tmp_path / "icon.icns")


# --- _build_app_bundle --------------------------------------------------


def test_build_app_bundle_writes_plists_and_compiles(tmp_path, monkeypatch):
    bundle = tmp_path / "App.app"
    monkeypatch.setattr(launchd, "APP_BUNDLE", bundle)
    with patch.object(launchd, "_build_icns"), \
         patch.object(launchd, "_xcrun", return_value=_proc()):
        launchd._build_app_bundle()
    assert (bundle / "Contents" / "Info.plist").exists()
    assert (bundle / "Contents" / "Library" / "LaunchAgents" / launchd._PLIST_NAME).exists()


def test_build_app_bundle_skips_icon_when_source_missing(tmp_path, monkeypatch):
    bundle = tmp_path / "App.app"
    monkeypatch.setattr(launchd, "APP_BUNDLE", bundle)
    monkeypatch.setattr(launchd, "_ICON_SOURCE", tmp_path / "no-icon.png")
    with patch.object(launchd, "_build_icns") as build_icns, \
         patch.object(launchd, "_xcrun", return_value=_proc()):
        launchd._build_app_bundle()
    build_icns.assert_not_called()
    assert "CFBundleIconFile" not in (bundle / "Contents" / "Info.plist").read_text()


def test_build_app_bundle_raises_when_helper_source_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "APP_BUNDLE", tmp_path / "App.app")
    monkeypatch.setattr(launchd, "_SWIFT_SOURCE", tmp_path / "missing.swift")
    with patch.object(launchd, "_build_icns"):
        with pytest.raises(launchd.LaunchdError, match="helper source is missing"):
            launchd._build_app_bundle()


def test_build_app_bundle_raises_when_compile_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "APP_BUNDLE", tmp_path / "App.app")
    with patch.object(launchd, "_build_icns"), \
         patch.object(launchd, "_xcrun", return_value=_proc(returncode=1, stderr="nope")):
        with pytest.raises(launchd.LaunchdError, match="compile the launch helper"):
            launchd._build_app_bundle()


def test_build_app_bundle_raises_when_signing_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "APP_BUNDLE", tmp_path / "App.app")
    # swiftc succeeds, codesign (the second _xcrun call) fails.
    with patch.object(launchd, "_build_icns"), \
         patch.object(launchd, "_xcrun", side_effect=[_proc(), _proc(returncode=1, stderr="x")]):
        with pytest.raises(launchd.LaunchdError, match="sign"):
            launchd._build_app_bundle()


# --- _run_helper --------------------------------------------------------


def test_run_helper_raises_when_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "_helper_path", lambda: tmp_path / "missing")
    with pytest.raises(launchd.LaunchdError, match="not installed"):
        launchd._run_helper("status")


def test_run_helper_returns_stdout_on_success(tmp_path, monkeypatch):
    helper = tmp_path / "helper"
    helper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(launchd, "_helper_path", lambda: helper)
    with patch.object(launchd, "_run", return_value=_proc(stdout=" enabled \n")):
        assert launchd._run_helper("status") == "enabled"


def test_run_helper_raises_on_nonzero_exit(tmp_path, monkeypatch):
    helper = tmp_path / "helper"
    helper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(launchd, "_helper_path", lambda: helper)
    with patch.object(launchd, "_run", return_value=_proc(returncode=1, stderr="bad")):
        with pytest.raises(launchd.LaunchdError, match="`register` failed: bad"):
            launchd._run_helper("register")


# --- _is_loaded / _require_loaded --------------------------------------


def test_is_loaded_true_when_launchctl_succeeds():
    with patch.object(launchd, "_run", return_value=_proc(returncode=0)):
        assert launchd._is_loaded() is True


def test_is_loaded_false_when_launchctl_fails():
    with patch.object(launchd, "_run", return_value=_proc(returncode=1)):
        assert launchd._is_loaded() is False


def test_require_loaded_passes_when_loaded():
    with patch.object(launchd, "_is_loaded", return_value=True):
        launchd._require_loaded()  # no raise


def test_require_loaded_raises_when_not_loaded():
    with patch.object(launchd, "_is_loaded", return_value=False):
        with pytest.raises(launchd.LaunchdError, match="not loaded"):
            launchd._require_loaded()


# --- install / start / restart / stop / status / uninstall -------------


def test_install_builds_bundle_and_registers(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(launchd, "APP_BUNDLE", tmp_path / "App.app")
    with patch.object(launchd, "_build_app_bundle") as build, \
         patch.object(launchd, "_run_helper", side_effect=[None, "enabled"]) as helper:
        assert launchd.install() == "enabled"
    build.assert_called_once()
    assert helper.call_count == 2


def test_start_kickstarts_when_already_loaded():
    with patch.object(launchd, "_is_loaded", return_value=True), \
         patch.object(launchd, "_run") as run:
        launchd.start()
    assert run.call_args[0][0][:2] == ["launchctl", "kickstart"]


def test_start_bootstraps_when_not_loaded(tmp_path, monkeypatch):
    plist = tmp_path / "agent.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(launchd, "_agent_plist_path", lambda: plist)
    with patch.object(launchd, "_is_loaded", return_value=False), \
         patch.object(launchd, "_run") as run:
        launchd.start()
    assert run.call_args[0][0][1] == "bootstrap"


def test_stop_boots_out_when_loaded():
    with patch.object(launchd, "_is_loaded", return_value=True), \
         patch.object(launchd, "_run") as run:
        launchd.stop()
    assert run.call_args[0][0][1] == "bootout"


def test_restart_kickstarts_with_kill_flag():
    with patch.object(launchd, "_is_loaded", return_value=True), \
         patch.object(launchd, "_run") as run:
        launchd.restart()
    assert run.call_args[0][0] == ["launchctl", "kickstart", "-k", launchd._service_target()]


def test_status_reports_not_installed_when_helper_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "_helper_path", lambda: tmp_path / "missing")
    assert launchd.status() == "not installed"


def test_status_reports_registered_but_not_loaded(tmp_path, monkeypatch):
    helper = tmp_path / "helper"
    helper.write_text("x")
    monkeypatch.setattr(launchd, "_helper_path", lambda: helper)
    with patch.object(launchd, "_run_helper", return_value="enabled"), \
         patch.object(launchd, "_run", return_value=_proc(returncode=1)):
        assert launchd.status() == "registered: enabled; not loaded"


def test_status_reports_running_state_from_launchctl(tmp_path, monkeypatch):
    helper = tmp_path / "helper"
    helper.write_text("x")
    monkeypatch.setattr(launchd, "_helper_path", lambda: helper)
    printout = "service = {\n\tstate = running\n}"
    with patch.object(launchd, "_run_helper", return_value="enabled"), \
         patch.object(launchd, "_run", return_value=_proc(returncode=0, stdout=printout)):
        assert launchd.status() == "registered: enabled; state = running"


def test_status_reports_loaded_when_no_state_line(tmp_path, monkeypatch):
    helper = tmp_path / "helper"
    helper.write_text("x")
    monkeypatch.setattr(launchd, "_helper_path", lambda: helper)
    with patch.object(launchd, "_run_helper", return_value="enabled"), \
         patch.object(launchd, "_run", return_value=_proc(returncode=0, stdout="no state here")):
        assert launchd.status() == "registered: enabled; loaded"


def test_uninstall_unregisters_and_removes_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / "App.app"
    helper = bundle / "Contents" / "MacOS" / launchd._HELPER_NAME
    helper.parent.mkdir(parents=True)
    helper.write_text("x")
    monkeypatch.setattr(launchd, "APP_BUNDLE", bundle)
    monkeypatch.setattr(launchd, "_helper_path", lambda: helper)
    with patch.object(launchd, "_run_helper") as helper_call:
        launchd.uninstall()
    helper_call.assert_called_once_with("unregister")
    assert not bundle.exists()


def test_uninstall_ignores_unregister_failure(tmp_path, monkeypatch):
    bundle = tmp_path / "App.app"
    helper = bundle / "Contents" / "MacOS" / launchd._HELPER_NAME
    helper.parent.mkdir(parents=True)
    helper.write_text("x")
    monkeypatch.setattr(launchd, "APP_BUNDLE", bundle)
    monkeypatch.setattr(launchd, "_helper_path", lambda: helper)
    with patch.object(launchd, "_run_helper", side_effect=launchd.LaunchdError("nope")):
        launchd.uninstall()  # best effort — still removes the bundle
    assert not bundle.exists()


def test_uninstall_when_helper_absent_just_removes_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / "App.app"
    bundle.mkdir()
    monkeypatch.setattr(launchd, "APP_BUNDLE", bundle)
    monkeypatch.setattr(launchd, "_helper_path", lambda: bundle / "missing")
    launchd.uninstall()
    assert not bundle.exists()
