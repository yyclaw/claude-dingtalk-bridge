from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .display import format_uptime

LABEL = "com.claude-dingtalk-bridge"
APP_NAME = "Claude DingTalk Bridge"
APP_BUNDLE = Path.home() / "Applications" / f"{APP_NAME}.app"
LOG_DIR = Path.home() / "Library" / "Logs" / "claude-dingtalk-bridge"

# macOS 26 forbids regular processes from writing ~/Library/LaunchAgents, so
# the launch agent ships *inside* APP_BUNDLE and is registered through
# SMAppService. A small Swift helper (compiled at install time) makes the
# SMAppService calls and also execs the daemon when launchd starts the agent.
_HELPER_NAME = "claude-dingtalk-bridge"
_PLIST_NAME = f"{LABEL}.plist"
_SWIFT_SOURCE = Path(__file__).parent / "resources" / "AppHelper.swift"

# Optional app icon. When resources/icon.png exists it is turned into a
# multi-resolution .icns at install time so System Settings shows it.
_ICON_SOURCE = Path(__file__).parent / "resources" / "icon.png"
_ICONSET_SIZES = (
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
)


class LaunchdError(Exception):
    """A daemon-management operation failed in a way the user can act on."""


def _run(args: list[str], check: bool = True, capture: bool = False):
    return subprocess.run(args, check=check, capture_output=capture, text=True)


def _service_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def _helper_path() -> Path:
    return APP_BUNDLE / "Contents" / "MacOS" / _HELPER_NAME


def _agent_plist_path() -> Path:
    return APP_BUNDLE / "Contents" / "Library" / "LaunchAgents" / _PLIST_NAME


def app_info_plist(icon_file: str | None = None) -> str:
    icon_entry = (
        f"    <key>CFBundleIconFile</key><string>{icon_file}</string>\n"
        if icon_file
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>{APP_NAME}</string>
    <key>CFBundleDisplayName</key><string>{APP_NAME}</string>
    <key>CFBundleIdentifier</key><string>{LABEL}</string>
    <key>CFBundleExecutable</key><string>{_HELPER_NAME}</string>
{icon_entry}    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>LSUIElement</key><true/>
</dict>
</plist>
"""


def agent_plist_content() -> str:
    out_log = LOG_DIR / "daemon.out.log"
    err_log = LOG_DIR / "daemon.err.log"
    # The agent runs the bundled helper in `run` mode, which execs the daemon.
    program = [
        str(_helper_path()),
        "run",
        sys.executable,
        "-m",
        "claude_dingtalk_bridge",
    ]
    arguments = "".join(f"        <string>{arg}</string>\n" for arg in program)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{arguments}    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{out_log}</string>
    <key>StandardErrorPath</key><string>{err_log}</string>
</dict>
</plist>
"""


def _xcrun(args: list[str], what: str) -> subprocess.CompletedProcess:
    try:
        return _run(["xcrun", *args], check=False, capture=True)
    except FileNotFoundError as exc:
        raise LaunchdError(
            f"Cannot {what}: Xcode command line tools are missing.\n"
            "Install them with `xcode-select --install`, then re-run install."
        ) from exc


def _build_icns(dest: Path) -> None:
    # sips and iconutil are macOS built-ins, so no Xcode tooling is needed.
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for name, size in _ICONSET_SIZES:
            resized = _run(
                ["sips", "-z", str(size), str(size), str(_ICON_SOURCE),
                 "--out", str(iconset / name)],
                check=False,
                capture=True,
            )
            if resized.returncode != 0:
                detail = (resized.stderr or resized.stdout or "").strip()
                raise LaunchdError(f"Failed to resize the app icon:\n{detail}")
        built = _run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(dest)],
            check=False,
            capture=True,
        )
        if built.returncode != 0:
            detail = (built.stderr or built.stdout or "").strip()
            raise LaunchdError(f"Failed to build the app icon:\n{detail}")


def _build_app_bundle() -> None:
    # Recreate from scratch so a stale helper, plist or signature never lingers.
    shutil.rmtree(APP_BUNDLE, ignore_errors=True)
    macos_dir = APP_BUNDLE / "Contents" / "MacOS"
    agents_dir = APP_BUNDLE / "Contents" / "Library" / "LaunchAgents"
    macos_dir.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)

    icon_file = None
    if _ICON_SOURCE.exists():
        resources_dir = APP_BUNDLE / "Contents" / "Resources"
        resources_dir.mkdir(parents=True, exist_ok=True)
        _build_icns(resources_dir / "icon.icns")
        icon_file = "icon"

    (APP_BUNDLE / "Contents" / "Info.plist").write_text(app_info_plist(icon_file))
    (agents_dir / _PLIST_NAME).write_text(agent_plist_content())

    if not _SWIFT_SOURCE.exists():
        raise LaunchdError(f"Bundled helper source is missing: {_SWIFT_SOURCE}")
    compiled = _xcrun(
        [
            "swiftc",
            str(_SWIFT_SOURCE),
            "-O",
            "-framework",
            "ServiceManagement",
            "-o",
            str(macos_dir / _HELPER_NAME),
        ],
        "compile the launch helper",
    )
    if compiled.returncode != 0:
        detail = (compiled.stderr or compiled.stdout or "").strip()
        raise LaunchdError(f"Failed to compile the launch helper:\n{detail}")

    # SMAppService requires the bundle to carry a signature; ad-hoc is fine
    # for a locally built app. Sign last, so it seals the helper and plist.
    signed = _xcrun(
        ["codesign", "--force", "--sign", "-", str(APP_BUNDLE)],
        "sign the app bundle",
    )
    if signed.returncode != 0:
        detail = (signed.stderr or signed.stdout or "").strip()
        raise LaunchdError(f"Failed to sign {APP_NAME}.app:\n{detail}")


def _run_helper(mode: str) -> str:
    helper = _helper_path()
    if not helper.exists():
        raise LaunchdError(
            "The service is not installed. Run `make daemon-install` first."
        )
    result = _run([str(helper), mode], check=False, capture=True)
    out = (result.stdout or "").strip()
    if result.returncode != 0:
        detail = (result.stderr or out or f"exit code {result.returncode}").strip()
        raise LaunchdError(f"`{mode}` failed: {detail}")
    return out


def _is_loaded() -> bool:
    return _run(["launchctl", "print", _service_target()], check=False, capture=True).returncode == 0


def _require_loaded() -> None:
    if not _is_loaded():
        raise LaunchdError(
            "Service is not loaded in launchd.\n"
            "Run `make daemon-install` first to register and start it."
        )


def install() -> str:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    APP_BUNDLE.parent.mkdir(parents=True, exist_ok=True)
    _build_app_bundle()
    _run_helper("register")
    return _run_helper("status")


def start() -> None:
    if _is_loaded():
        _run(["launchctl", "kickstart", _service_target()])
        return
    # Booted out earlier — bootstrap the bundled plist back in. This reads the
    # plist from inside the .app and never touches ~/Library/LaunchAgents.
    if not _agent_plist_path().exists():
        raise LaunchdError(
            "Service is not installed. Run `make daemon-install` first."
        )
    _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(_agent_plist_path())])


def stop() -> None:
    if not _is_loaded():
        raise LaunchdError(
            "Service is not loaded in launchd — nothing to stop.\n"
            "Run `make daemon-install` if you meant to set it up."
        )
    _run(["launchctl", "bootout", _service_target()])


def restart() -> None:
    if not _is_loaded():
        start()
        return
    _run(["launchctl", "kickstart", "-k", _service_target()])


def _parse_etime(value: str) -> int:
    """Parse a ps(1) ``etime`` string (``[[dd-]hh:]mm:ss``) into seconds."""
    days = 0
    if "-" in value:
        day_str, value = value.split("-", 1)
        days = int(day_str)
    fields = [int(f) for f in value.split(":")]
    while len(fields) < 3:
        fields.insert(0, 0)
    hours, minutes, seconds = fields
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _process_uptime_seconds(pid: str) -> int | None:
    """Elapsed seconds since ``pid`` started, or None if it can't be read."""
    result = _run(["ps", "-o", "etime=", "-p", str(pid)], check=False, capture=True)
    etime = result.stdout.strip()
    if result.returncode != 0 or not etime:
        return None
    try:
        return _parse_etime(etime)
    except ValueError:
        return None


def status() -> str:
    if not _helper_path().exists():
        return "not installed"
    registration = _run_helper("status")
    result = _run(["launchctl", "print", _service_target()], check=False, capture=True)
    if result.returncode != 0:
        return f"login item: {registration}; state: not loaded"
    state = None
    pid = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        # Keep the first (top-level) state; nested sub-services repeat the key.
        if state is None and stripped.startswith("state ="):
            state = stripped.split("=", 1)[1].strip()
        elif pid is None and stripped.startswith("pid ="):
            pid = stripped.split("=", 1)[1].strip()
    base = f"login item: {registration}; state: {state or 'loaded'}"
    if pid:
        seconds = _process_uptime_seconds(pid)
        if seconds is not None:
            base += f"; up: {format_uptime(seconds)}"
    return base


def uninstall() -> None:
    if _helper_path().exists():
        try:
            _run_helper("unregister")
        except LaunchdError:
            pass  # best effort — still remove the bundle below
    shutil.rmtree(APP_BUNDLE, ignore_errors=True)
