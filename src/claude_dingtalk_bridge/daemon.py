from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import dingtalk_stream
import websockets
from dingtalk_stream import AckMessage

from claude_dingtalk_bridge.claude_runner import ClaudeRunner
from claude_dingtalk_bridge.config import Config, load_config
from claude_dingtalk_bridge.dingtalk import DingTalkTransport
from claude_dingtalk_bridge.geo import CachedGeoCheck
from claude_dingtalk_bridge.images import download_image
from claude_dingtalk_bridge.orchestrator import Orchestrator
from claude_dingtalk_bridge.permissions import PermissionPolicy
from claude_dingtalk_bridge.projects import ProjectRegistry

logger = logging.getLogger(__name__)

# Reconnect delay matches dingtalk_stream.start_forever's own 3s pause.
_RECONNECT_DELAY = 3.0


def _disable_websocket_proxy() -> None:
    """Make the DingTalk Stream WebSocket connect directly, bypassing proxies.

    websockets resolves a proxy from the OS network configuration, so a
    system-wide SOCKS proxy would route the connection through python-socks
    (not a dependency) and fail. DingTalk is reachable directly, but
    dingtalk_stream calls ``websockets.connect()`` without a ``proxy``
    argument, so the only hook is to wrap it and force ``proxy=None``.
    """
    original = websockets.connect

    def connect(*args, **kwargs):
        kwargs.setdefault("proxy", None)
        return original(*args, **kwargs)

    websockets.connect = connect


def _filter_client_noise() -> None:
    """Tame the ``dingtalk_stream.client`` SDK logger.

    The SDK calls ``logger.error()`` for two categories of event we don't
    want surfaced at ERROR level:

    * Clean shutdown: ``start()`` catches ``asyncio.CancelledError`` in the
      same ``except`` as real network errors. The exception is the first
      positional arg, so we match on its type (not the format string) and
      drop the record.
    * Auto-reconnect events (``[start] network exception``,
      ``open connection failed``): the SDK reconnects within seconds, so
      ERROR is alarmist. But they do signal an outage, so they belong in
      err.log (not out.log, which is for normal operational chatter) —
      downgrade to WARNING.
    """

    reconnect_hints = ("network exception", "open connection failed")

    class _Filter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            args = record.args
            if isinstance(args, tuple) and args and isinstance(
                args[0], asyncio.CancelledError
            ):
                return False
            if record.levelno >= logging.ERROR and isinstance(record.msg, str):
                if any(hint in record.msg for hint in reconnect_hints):
                    record.levelno = logging.WARNING
                    record.levelname = "WARNING"
            return True

    logging.getLogger("dingtalk_stream.client").addFilter(_Filter())


def build_orchestrator(config: Config) -> tuple[Orchestrator, DingTalkTransport]:
    registry = ProjectRegistry(config.projects)
    policy = PermissionPolicy(config.permissions)
    transport = DingTalkTransport(config.dingtalk_client_id, config.dingtalk_client_secret)
    user_id = config.authorized_user_id

    async def send(text: str) -> None:
        await asyncio.to_thread(transport.send_text, user_id, text)

    async def send_markdown(text: str) -> None:
        await asyncio.to_thread(transport.send_markdown, user_id, "Claude", text)

    runner = ClaudeRunner()

    geo_check = None
    if config.geo is not None:
        runner.proxy_url = config.geo.proxy_url
        cached_geo = CachedGeoCheck(config.geo)

        def geo_check():  # noqa: F811 - bound only when geo is configured
            return asyncio.to_thread(cached_geo)

    orchestrator = Orchestrator(
        config=config,
        registry=registry,
        policy=policy,
        runner=runner,
        send=send,
        send_markdown=send_markdown,
        geo_check=geo_check,
    )
    runner.permission_handler = orchestrator.request_permission
    runner.question_handler = orchestrator.answer_question
    return orchestrator, transport


def build_image_prompt(parts: list[tuple[str, str]]) -> str:
    """Assemble a Claude prompt from ordered image-message parts.

    Each part is `("text", text)` or `("image", local_path)`. Image parts are
    rendered inline as a path reference so Claude can open them with `Read`. A
    message with text gets a header announcing both; an image-only message
    (the `picture` case) gets a simpler single-image phrasing.
    """
    has_text = any(kind == "text" for kind, _ in parts)
    rendered = " ".join(
        value if kind == "text" else f"[image saved at {value}]"
        for kind, value in parts
    )
    if has_text:
        return f"I sent you a message with text and images:\n\n{rendered}"
    image_count = sum(1 for kind, _ in parts if kind == "image")
    noun = "an image" if image_count == 1 else f"{image_count} images"
    return f"I sent you {noun}: {rendered}. Please take a look."


def _mask_sender(sender: str | None) -> str:
    # Sender staff id can identify a real person; the inbound log is the only
    # place we routinely emit it, so mask to first-3 + ****** + last-3.
    if not sender:
        return "?"
    return f"{sender[:3]}******{sender[-3:]}"


def _log_inbound(msg: dingtalk_stream.ChatbotMessage) -> None:
    """Log a one-line summary of every inbound callback before dispatch.

    Single most important debugging aid: when the user reports "I sent a
    message and nothing happened", this line tells us whether the daemon ever
    saw the message and which msgtype it had. Preview is short and
    single-line to keep one message per log line.
    """
    sender = _mask_sender(msg.sender_staff_id)
    mt = msg.message_type
    if mt == "text":
        preview = (msg.text.content or "").strip().splitlines()[0:1]
        snippet = (preview[0] if preview else "")[:60]
        logger.info('inbound msgtype=text sender=%s preview="%s"', sender, snippet)
    elif mt == "richText":
        items = msg.rich_text_content.rich_text_list or []
        texts = sum(1 for it in items if it.get("text"))
        images = sum(1 for it in items if it.get("downloadCode"))
        other = len(items) - texts - images
        logger.info(
            "inbound msgtype=richText sender=%s text_items=%d image_items=%d other_items=%d",
            sender, texts, images, other,
        )
    else:
        logger.info("inbound msgtype=%s sender=%s", mt, sender)


class _ChatHandler(dingtalk_stream.ChatbotHandler):
    def __init__(self, orchestrator: Orchestrator):
        super().__init__()
        self._orchestrator = orchestrator

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        try:
            msg = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
            # Log every callback BEFORE dispatch — the next "I sent a message
            # and nothing happened" report can immediately confirm whether the
            # message reached the daemon at all (and which branch took it).
            _log_inbound(msg)
            if msg.message_type == "text":
                text = msg.text.content.strip()
                await self._orchestrator.handle_message(text, msg.sender_staff_id)
            elif msg.message_type == "audio":
                # dingtalk_stream doesn't parse `audio`; its content (with
                # DingTalk's own transcription) lands in extensions.
                content = msg.extensions.get("content") or {}
                await self._orchestrator.handle_audio(
                    content.get("recognition"), msg.sender_staff_id
                )
            elif msg.message_type in ("picture", "richText"):
                await self._handle_image_message(msg)
            else:
                # Unknown msgtype (file, link, sticker, future DingTalk types).
                # Authorized senders get a phone notice so a stuck message
                # doesn't look like a hung daemon; strangers get nothing.
                logger.warning(
                    "unsupported msgtype=%s sender=%s",
                    msg.message_type, msg.sender_staff_id,
                )
                if self._orchestrator.is_authorized(msg.sender_staff_id):
                    await self._orchestrator.notify(
                        f"🤔 Got a `{msg.message_type}` message — I can only "
                        f"handle text, voice, and images."
                    )
        except Exception:  # noqa: BLE001 - never let one bad message kill the loop
            logger.exception("Failed to handle inbound message")
        return AckMessage.STATUS_OK, "OK"

    async def _handle_image_message(self, msg: dingtalk_stream.ChatbotMessage):
        """Download a picture/richText message's images and run it as a turn.

        `picture` is the single-image, no-text case of `richText`. richText
        items are walked in order so text and images keep their interleaving;
        non-text non-image items (@-mentions, links) are dropped.
        """
        # Image download is the one inbound path with a side effect (network
        # fetch + disk write) before the orchestrator's own auth check, so an
        # unauthorized sender must be turned away here, before any download.
        if not self._orchestrator.is_authorized(msg.sender_staff_id):
            logger.warning(
                "Ignoring image message from unauthorized sender %s",
                msg.sender_staff_id,
            )
            return
        if msg.message_type == "picture":
            raw_parts = [("image", msg.image_content.download_code)]
        else:
            raw_parts = []
            for item in msg.rich_text_content.rich_text_list:
                if item.get("text"):
                    raw_parts.append(("text", item["text"]))
                elif item.get("downloadCode"):
                    raw_parts.append(("image", item["downloadCode"]))
        if not any(kind == "image" for kind, _ in raw_parts):
            # No image to download — but text items (a sticker / @-mention
            # bundled with text, etc.) used to fall on the floor here. Salvage
            # the text as a regular prompt so the user's intent isn't lost.
            text = "".join(v for k, v in raw_parts if k == "text").strip()
            if text:
                await self._orchestrator.handle_message(text, msg.sender_staff_id)
            return
        # Download all images concurrently; richText messages with multiple
        # pictures would otherwise pay N× single-image latency.
        parts: list[tuple[str, str]] = list(raw_parts)
        image_indices = [i for i, (kind, _) in enumerate(parts) if kind == "image"]
        results = await asyncio.gather(
            *(asyncio.to_thread(self._fetch_image, parts[i][1]) for i in image_indices),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                # Without this branch the upstream `except` in process() would
                # swallow the failure and the user would see nothing for their
                # image message.
                logger.warning("Image download failed: %s", result)
                await self._orchestrator.notify(
                    f"📷 Couldn't download an image from your message.  \n"
                    f"`{type(result).__name__}: {result}`"
                )
                return
        for idx, path in zip(image_indices, results):
            parts[idx] = ("image", path)
        await self._orchestrator.handle_image(
            build_image_prompt(parts), msg.sender_staff_id
        )

    def _fetch_image(self, download_code: str) -> str:
        """Resolve a downloadCode and download the image; return its file path."""
        url = self.get_image_download_url(download_code)
        return str(download_image(url))


def run() -> None:
    # launchd captures stdout → out.log, stderr → err.log. Default basicConfig
    # piles everything onto stderr, so out.log stays empty and err.log fills
    # with INFO chatter. Route INFO/DEBUG to stdout and WARNING+ to stderr.
    import sys

    # When stdout is redirected to a file (launchd), Python defaults to
    # block-buffered output — log lines pool in memory and only flush in
    # chunks, so `tail -f` shows bursts of lines whose asctime values
    # ('locked in at logger.info() time') look like the daemon froze and
    # then caught up. Force line-buffering so every record hits disk
    # immediately and `tail -f` is real-time.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, OSError):
            pass  # stream not a TextIOWrapper (e.g. tests); skip.

    from claude_dingtalk_bridge import log_context

    class _ShortNameFormatter(logging.Formatter):
        # `claude_dingtalk_bridge.orchestrator` → `orchestrator`; keeps the
        # last dotted segment so phone-side log readers aren't pushed off the
        # screen by the package prefix.
        # Also pulls the active session/turn from log_context and prepends
        # them right after the shortname column so each turn line reads as
        # `<ts> INFO <shortname> session=… turn=… <message>` — neat aligned
        # columns. Lines emitted outside any turn (daemon startup, websocket
        # events) get no prefix at all. `grep session=<prefix>` works either
        # position.
        def format(self, record: logging.LogRecord) -> str:
            record.shortname = record.name.rsplit(".", 1)[-1]
            session = log_context.session_label()
            turn = log_context.turn_label()
            if session == "-" and turn == "-":
                record.session_turn = ""
            else:
                record.session_turn = f"session={session} turn={turn} "
            return super().format(record)

    fmt = _ShortNameFormatter(
        "%(asctime)s %(levelname)s %(shortname)s %(session_turn)s%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setLevel(logging.DEBUG)
    stdout.addFilter(lambda r: r.levelno < logging.WARNING)
    stdout.setFormatter(fmt)
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.WARNING)
    stderr.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [stdout, stderr]
    # subprocess_cli logs one "Using bundled Claude Code CLI: <long-path>" line
    # per turn at INFO; the path never changes once the venv is built. Lift to
    # WARNING so settings/process warnings still surface but the per-turn noise
    # doesn't. Agent output is routed through claude_runner and unaffected.
    logging.getLogger(
        "claude_agent_sdk._internal.transport.subprocess_cli"
    ).setLevel(logging.WARNING)
    _filter_client_noise()
    config = load_config()
    _disable_websocket_proxy()
    orchestrator, _ = build_orchestrator(config)
    credential = dingtalk_stream.Credential(
        config.dingtalk_client_id, config.dingtalk_client_secret
    )
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(
        dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
        _ChatHandler(orchestrator),
    )
    logger.info("Starting DingTalk stream client")
    try:
        asyncio.run(_serve(client, orchestrator))
    except KeyboardInterrupt:
        # Reached when a signal arrived during a window asyncio could not
        # translate into our shutdown event — e.g. while interpreter teardown.
        logger.info("Shutting down")


async def _drive_stream_client(client) -> None:
    """Run the stream client, reconnecting on transient errors.

    Mirrors ``DingTalkStreamClient.start_forever``'s reconnect loop but as a
    cancellable coroutine, so the surrounding ``_serve`` can race it against
    a shutdown signal.
    """
    while True:
        try:
            await client.start()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - one bad connection mustn't kill the loop
            logger.exception("Stream client errored; reconnecting in %.0fs", _RECONNECT_DELAY)
        try:
            await asyncio.sleep(_RECONNECT_DELAY)
        except asyncio.CancelledError:
            return


async def _serve(client, orchestrator: Orchestrator) -> None:
    """Run the stream client until the OS asks us to stop, then drain cleanly.

    SIGTERM (launchd's stop signal) and SIGINT (Ctrl+C) both set a shutdown
    event rather than crashing the loop, so the orchestrator can resolve any
    pending permission/question waits and the SDK subprocess gets a chance
    to disconnect before the loop dies — otherwise the user's phone hangs
    and the Claude child becomes an orphan.

    Lifecycle notifications (started/stopped/restarted) are not sent here:
    SIGTERM looks the same for a stop and for a restart, so this layer has
    no way to label them correctly. The CLI (``cli.py``) owns those notices
    because the user's intent is unambiguous at the command boundary.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Some environments (Windows, sub-thread loops, pytest fixtures)
            # disallow add_signal_handler; fall back to default semantics.
            pass

    client_task = asyncio.create_task(_drive_stream_client(client))
    stop_wait = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait(
            {client_task, stop_wait}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        stop_wait.cancel()
        with contextlib.suppress(BaseException):
            await stop_wait
        try:
            await orchestrator.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("Orchestrator shutdown raised")
        client_task.cancel()
        with contextlib.suppress(BaseException):
            await client_task
