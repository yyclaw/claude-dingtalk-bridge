from __future__ import annotations

import argparse
import sys

from claude_dingtalk_bridge import launchd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude-dingtalk-bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "start", "stop", "restart", "status", "uninstall"):
        sub.add_parser(name)
    args = parser.parse_args(argv)

    try:
        return _dispatch(args)
    except launchd.LaunchdError as exc:
        print(f"✋ {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "install":
        registration = launchd.install()
        if registration == "requiresApproval":
            print(
                "Installed. macOS needs your approval — enable "
                f"“{launchd.APP_NAME}” in "
                "System Settings › General › Login Items."
            )
        else:
            print(f"Installed and registered ({registration}).")
    elif args.command == "start":
        launchd.start()
        _notify_phone("✅ Daemon started.")
        print("Started.")
    elif args.command == "stop":
        launchd.stop()
        _notify_phone("🛑 Daemon stopped.")
        print("Stopped (booted out of launchd; KeepAlive will not relaunch it).")
    elif args.command == "restart":
        launchd.restart()
        _notify_phone("🔄 Daemon restarted.")
        print("Restarted.")
    elif args.command == "status":
        print(launchd.status())
    elif args.command == "uninstall":  # pragma: no branch
        # Argparse's `required=True` rules out any other command value,
        # so the implicit False arm of this elif is unreachable.
        launchd.uninstall()
        print("Uninstalled.")
    return 0


def _notify_phone(message: str) -> None:
    """Push a lifecycle notification to the phone over the REST transport.

    The daemon itself can't tell ``stop`` from ``restart`` -- both arrive as
    SIGTERM -- so these messages are sent here, where the user's intent is
    captured by the chosen subcommand. Imports are local so commands that
    don't notify (``status``, ``install``, ``uninstall``) don't pay for the
    YAML/requests pull-in. A missing config or a transport error is reported
    to stderr but never fails the CLI: the launchd action already succeeded
    and the phone notice is secondary.
    """
    try:
        from claude_dingtalk_bridge.config import load_config
        from claude_dingtalk_bridge.dingtalk import DingTalkTransport

        config = load_config()
        transport = DingTalkTransport(
            config.dingtalk_client_id, config.dingtalk_client_secret
        )
        transport.send_text(config.authorized_user_id, message)
    except Exception as exc:  # noqa: BLE001 - never block the CLI on phone push
        print(f"(could not send phone notice: {exc})", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
