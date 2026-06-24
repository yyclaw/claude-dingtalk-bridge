from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import signal
import time
from collections.abc import Awaitable, Callable
from urllib.parse import quote_plus

import dingtalk_stream
import requests
import websockets
from dingtalk_stream import AckMessage
from dingtalk_stream.stream import DingTalkStreamClient
from dingtalk_stream.version import VERSION_STRING

from claude_dingtalk_bridge.chunking import chunk_markdown, pad_code_tail
from claude_dingtalk_bridge.claude_runner import ClaudeRunner
from claude_dingtalk_bridge.config import Config, load_config
from claude_dingtalk_bridge.connectivity import (
    WAKE_SKEW_THRESHOLD,
    wake_is_dark,
    watch_reachability,
    watch_wake,
)
from claude_dingtalk_bridge.dingtalk import DingTalkTransport
from claude_dingtalk_bridge.display import display_path, format_duration
from claude_dingtalk_bridge.geo import CachedGeoCheck
from claude_dingtalk_bridge.images import download_image
from claude_dingtalk_bridge.orchestrator import Orchestrator
from claude_dingtalk_bridge.projects import ProjectRegistry
from claude_dingtalk_bridge.stream_reconnect import ReconnectState
from claude_dingtalk_bridge import self_update

logger = logging.getLogger(__name__)

_OPEN_CONNECTION_TIMEOUT = (5.0, 10.0)  # connect, read

# DingTalk caps a rendered robot message two independent ways (measured
# 2026-06-02). Over ~20000 characters oToMessages/batchSend hard-rejects (HTTP
# 400) and the reply is dropped, not clipped; the char count is what matters,
# but we budget in UTF-8 bytes (always >= the char count) so a pure-ASCII chunk
# still stays well under 20000.
_MARKDOWN_BYTE_BUDGET = 16000

# Separately, a message holding a long code block silently loses its last few
# rendered lines — even behind the "expand" control — and the loss grows with
# length (~1 line near 35, ~6 near 150). Two defenses work together: cap each
# chunk's lines so that growing drop stays small and bounded, and pad a code
# chunk's tail (below) by the length-scaled estimate so the drop eats filler,
# not code.
_MARKDOWN_LINE_BUDGET = 200
_CODE_TAIL_MIN_LINES = 35  # below this a code chunk renders whole; don't pad
_CODE_TAIL_MARGIN = 2  # safety blanks beyond the length-scaled drop estimate

# Daily self-update check. The first check waits a short delay so the network
# and stream connection settle after startup; subsequent ones run once a day.
_AUTO_UPDATE_INITIAL_DELAY = 60
_AUTO_UPDATE_CHECK_INTERVAL = 24 * 3600

# Below this many seconds, a reconnect gap self-heals fast enough that it isn't
# worth nagging the phone about — only longer outages (system sleep, gateway
# lockout) risk dropping inbound messages, which DingTalk Stream never replays.
_OFFLINE_NOTICE_THRESHOLD = 30.0

# On shutdown, give an in-flight offline-recovery notice this long to finish its
# send before tearing it down — the notice tells the phone messages may have been
# dropped, so it's worth a brief wait, but a hung transport mustn't stall exit.
_SHUTDOWN_NOTICE_TIMEOUT = 3.0


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


_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")


def _extract_title(text: str) -> str | None:
    """Lift the first heading line as the DingTalk markdown title.

    The title is what shows in the chat-list preview; we leave the `###`
    line in the body so the on-screen header stays visible. Stops at the
    first non-empty line — only a leading heading counts.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _HEADING_RE.match(stripped)
        return match.group(1).strip() if match else None
    return None


def build_orchestrator(config: Config) -> tuple[Orchestrator, DingTalkTransport]:
    registry = ProjectRegistry(config.projects)
    transport = DingTalkTransport(config.dingtalk_client_id, config.dingtalk_client_secret)
    user_id = config.authorized_user_id

    async def send(text: str) -> None:
        await asyncio.to_thread(transport.send_text, user_id, text)

    async def send_markdown(text: str) -> None:
        for piece in chunk_markdown(
            text, _MARKDOWN_BYTE_BUDGET, _MARKDOWN_LINE_BUDGET
        ):
            piece = pad_code_tail(
                piece, _CODE_TAIL_MIN_LINES, _CODE_TAIL_MARGIN
            )
            title = _extract_title(piece) or "Claude has replied."
            await asyncio.to_thread(transport.send_markdown, user_id, title, piece)

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
        value if kind == "text" else f"[image saved at {display_path(value)}]"
        for kind, value in parts
    )
    if has_text:
        return f"I sent you a message with text and images:\n\n{rendered}"
    image_count = sum(1 for kind, _ in parts if kind == "image")
    noun = "an image" if image_count == 1 else f"{image_count} images"
    return f"I sent you {noun}: {rendered}. Please take a look."


def _mask_sender(sender: str | None) -> str:
    # Sender staff id can identify a real person; the inbound log is the only
    # place we routinely emit it, so mask to first-2 + *** + last-2.
    if not sender:
        return "?"
    # first-2 + last-2 only hides anything when the two slices clear each other
    # with a gap (len > 4); shorter ids would expose or duplicate the whole
    # value, so mask them entirely.
    if len(sender) <= 4:
        return "***"
    return f"{sender[:2]}***{sender[-2:]}"


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
                        f"🤔 Got a {msg.message_type} message — I can only "
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
                    f"📷 Couldn't download an image from your message.\n"
                    f"{type(result).__name__}: {result}"
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


def _open_connection(client) -> dict | None:
    """Request a Stream gateway endpoint with a request timeout.

    The SDK's own ``open_connection`` calls ``requests.post`` without a
    timeout, so a half-dead network can block it indefinitely and starve
    the reconnect loop.
    """
    url = DingTalkStreamClient.OPEN_CONNECTION_API
    logger.info("open connection, url=%s", url)
    topics: list[dict] = []
    if client._is_event_required:
        topics.append({"type": "EVENT", "topic": "*"})
    for topic in client.callback_handler_map.keys():
        topics.append({"type": "CALLBACK", "topic": topic})
    body = json.dumps({
        "clientId": client.credential.client_id,
        "clientSecret": client.credential.client_secret,
        "subscriptions": topics,
        "ua": f"dingtalk-sdk-python/v{VERSION_STRING}-union",
        "localIp": client.get_host_ip(),
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    try:
        resp = requests.post(
            url,
            headers=headers,
            data=body,
            timeout=_OPEN_CONNECTION_TIMEOUT,
            # Bypass the ambient/system proxy: requests honors the macOS system
            # proxy (and http_proxy env) by default, but DingTalk is reached
            # directly — only Claude's task traffic and the geo check ride the
            # geo proxy. This mirrors _disable_websocket_proxy for the WS that
            # this very call bootstraps.
            proxies={"http": None, "https": None},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("open connection failed: %s", e)
        return None


async def _serve_stream_once(
    client,
    *,
    on_connect: Callable[[float], None] | None = None,
    on_message: Callable[[], None] | None = None,
) -> float | None:
    """Open the gateway and serve the websocket until it closes.

    Single-pass — the daemon's outer loop owns retry/backoff policy, so the
    SDK's own ``start()`` (which embeds a 10s flat retry that triggers
    gateway lockouts) can't be used directly.

    ``on_connect`` (if given) is called with the wall-clock open time the moment
    the websocket comes up, so the outer loop can measure how long it was down.
    ``on_message`` (if given) fires on each inbound frame — proof the socket is
    genuinely alive — so the loop can drop a stale wake/retry signal that the
    connection outlived (see ``_drive_stream_client``).

    Returns the **wall-clock** seconds the websocket stayed live, or ``None``
    if the connection never came up. Wall-clock (not monotonic) so a connection
    that spanned a system sleep counts as long-lived for the backoff stability
    check — monotonic pauses during sleep and would make it look short, falsely
    ratcheting the backoff. Re-raises ``CancelledError``; logs and absorbs all
    other failures so the outer loop just sees "no connection".
    """
    client.pre_start()
    connection = await asyncio.to_thread(_open_connection, client)
    if connection is None:
        return None
    logger.info("endpoint is %s", connection)
    uri = f"{connection['endpoint']}?ticket={quote_plus(connection['ticket'])}"

    opened_at: float | None = None
    try:
        async with websockets.connect(uri) as websocket:
            opened_at = time.time()
            client.websocket = websocket
            if on_connect is not None:
                on_connect(opened_at)
            keepalive = asyncio.create_task(client.keepalive(websocket))
            try:
                async for raw in websocket:
                    if on_message is not None:
                        try:
                            on_message()
                        except Exception:  # noqa: BLE001 - a liveness hook must not drop a live socket
                            logger.warning("on_message hook raised", exc_info=True)
                    asyncio.create_task(
                        client.background_task(json.loads(raw))
                    )
            finally:
                keepalive.cancel()
                with contextlib.suppress(BaseException):
                    await keepalive
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("stream connection error: %s", e)
    if opened_at is None:
        return None
    return time.time() - opened_at


async def _drive_stream_client(
    client,
    *,
    state: ReconnectState | None = None,
    retry_now: asyncio.Event | None = None,
    disconnected: asyncio.Event | None = None,
    on_recovered: Callable[[float], None] | None = None,
    is_dark_wake: Callable[[], Awaitable[bool]] = wake_is_dark,
    wall_clock: Callable[[], float] | None = None,
    mono_clock: Callable[[], float] | None = None,
) -> None:
    """Run the stream client with exponential backoff between attempts.

    Each cycle calls ``_serve_stream_once`` for one connection attempt and feeds
    the result to ``ReconnectState``, which computes the next delay. The backoff
    wait is interruptible: a watcher setting ``retry_now`` (a system wake or a
    network return) abandons the wait so the loop can reconnect at once.

    The signal is *classified* before acting (``_backoff_until_retry``): a macOS
    DarkWake — which also trips ``retry_now`` — stays in backoff, while a full
    user wake or a genuine network return reconnects and resets the ladder. The
    ``pmset`` probe behind that runs only here, while we're actually deciding, so
    a connected daemon never forks one per overnight maintenance wake.

    ``disconnected`` is set while waiting and cleared while a connection is live,
    so the reachability watcher only probes when we're down.

    ``on_recovered`` (if given) is called with the offline duration in seconds
    whenever a reconnection lands after an outage longer than
    ``_OFFLINE_NOTICE_THRESHOLD`` — the daemon was deaf to inbound messages for
    that window and DingTalk never replays them, so the phone is told to resend.
    The startup connection (no prior online session) never fires it.

    The outage is the *sum* of two back-to-back segments: the suspend measured
    from a wall-vs-monotonic clock baseline (the sleep *before* the socket gave
    out) plus the live ``down_since`` gap (the socket-down wall time *after*).
    Sleep is detected lazily — a frozen socket only fails its keepalive *after*
    wake, so ``down_since`` alone would clock a multi-hour sleep as a few seconds;
    the clock skew (wall advances while monotonic is frozen during sleep) recovers
    that pre-drop suspend, while ``down_since`` covers the post-drop span (awake
    network outage, or a later sleep the dead socket sat through). Summing — not
    ``max`` — is what makes a "slept, socket survived a while, then died, stayed
    down" outage report the *whole* unreachable span (≈ sleep → wake), since the
    phone got nothing that entire time. For a pure awake outage the suspend is ~0
    (gap dominates); for a pure wake-driven reconnect the gap is ~0 (suspend
    dominates). The baseline is set on connect and refreshed on every inbound
    frame (``on_message``) — proof the socket is alive *now* — so a sleep the
    socket *fully* survives (frames resume after) is forgotten; only the suspend
    since the last frame, i.e. the one that led to the drop, is counted.

    When that measured suspend exceeds ``WAKE_SKEW_THRESHOLD`` the disconnect was
    wake-induced: the loop self-nudges ``retry_now`` so ``_backoff_until_retry``
    classifies it at once (a full wake reconnects now, a DarkWake stays in backoff)
    rather than waiting the delay out — keeping a lid-open reconnect prompt without
    a watcher having to fire. An awake outage measures ~0 suspend and backs off
    normally. ``wall_clock``/``mono_clock`` default to ``time.time``/
    ``time.monotonic`` (resolved at call so tests can monkeypatch the module) and
    are injectable so the suspend measurement is unit-testable.
    """
    state = state or ReconnectState()
    retry_now = retry_now or asyncio.Event()
    disconnected = disconnected or asyncio.Event()
    wall_clock = wall_clock or time.time
    mono_clock = mono_clock or time.monotonic
    down_since: float | None = None
    online = False
    # Wall/monotonic baseline of the last moment the socket was known alive, used
    # to measure a suspend by clock skew at disconnect. ``None`` until first connect.
    alive_wall: float | None = None
    alive_mono = 0.0
    # Suspend measured at the last disconnect, consumed by the next connect's notice.
    slept_pending = 0.0

    def _on_connect(opened_at: float) -> None:
        nonlocal down_since, online, alive_wall, alive_mono, slept_pending
        online = True
        # A signal that fired during the connect attempt is moot now we're up;
        # clear it so it can't skip the next, unrelated disconnect's backoff.
        retry_now.clear()
        prev, down_since = down_since, None
        slept, slept_pending = slept_pending, 0.0
        alive_wall, alive_mono = wall_clock(), mono_clock()
        if prev is None:
            # Startup connect: no prior online session, so this is not a
            # recovery — never fire the notice, however large `slept` is.
            return
        gap = opened_at - prev
        # The pre-drop suspend (`slept`) and the socket-down wall gap (`gap`) are
        # two back-to-back offline segments — sum them for the true outage (≈ the
        # span from going to sleep until waking). For a pure awake outage slept≈0;
        # for a pure wake-driven reconnect gap≈0; only a survived sleep that then
        # dies has both, where `max` would wrongly drop the earlier sleep.
        offline = slept + gap
        if on_recovered is not None and offline >= _OFFLINE_NOTICE_THRESHOLD:
            on_recovered(offline)

    def _on_message() -> None:
        # An inbound frame proves the socket alive *now*: refresh the baseline so a
        # sleep the socket outlived (before this frame) isn't counted as an outage.
        nonlocal alive_wall, alive_mono
        alive_wall, alive_mono = wall_clock(), mono_clock()

    while True:
        disconnected.clear()
        try:
            duration = await _serve_stream_once(
                client, on_connect=_on_connect, on_message=_on_message
            )
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - one bad pass mustn't kill the loop
            logger.exception("Stream connection raised; treating as failure")
            duration = None
        if online:
            down_since = wall_clock()
            online = False
            logger.info("stream connection lost")
            # Measure the suspend ONLY on this online→offline edge. A failing
            # reconnect (e.g. DNS not ready on wake) never refreshes `alive_wall`,
            # so re-measuring it on every failed pass would keep reading the same
            # stale skew and re-firing the self-nudge — bypassing backoff and
            # spinning open-connection until DNS recovers. `alive_wall` is always
            # set here: `online` only becomes True via `_on_connect`, which sets it.
            # Skew = wall elapsed minus monotonic elapsed = time suspended since
            # last known alive (awake time advances both, cancels).
            slept_pending = max(
                0.0, (wall_clock() - alive_wall) - (mono_clock() - alive_mono)
            )
            if slept_pending > WAKE_SKEW_THRESHOLD:
                # A wake killed the socket: classify the signal now (full wake →
                # reconnect at once) instead of waiting the backoff out.
                retry_now.set()
        delay = state.on_disconnect(duration)
        disconnected.set()
        outcome = await _backoff_until_retry(delay, retry_now, is_dark_wake)
        if outcome is None:
            return  # cancelled
        if outcome:
            state.reset()


async def _backoff_until_retry(
    delay: float,
    retry_now: asyncio.Event,
    is_dark_wake: Callable[[], Awaitable[bool]],
) -> bool | None:
    """Wait out ``delay`` of backoff, cutting it short for a genuine wake/return.

    Each ``retry_now`` signal — set by a system wake *or* a network return — is
    classified once via ``is_dark_wake``: a macOS DarkWake (radios briefly up for
    maintenance) keeps backing off the *remaining* time; a full wake or a real
    network return reconnects at once. A signal already pending on entry fired
    while the dead socket was still being served, so it's classified before any
    wait.

    Crucially, a drained backoff is *also* gated on the wake classification, not
    just a wake-induced ``retry_now``. The backoff clock is monotonic CPU time,
    which a sleeping machine still accrues in brief DarkWake slivers; left
    ungated it would expire mid-sleep and reconnect into the Power Nap window
    that fleetingly raised the radios — succeeding, firing a "reconnected"
    notice, then refreezing, and flapping the phone all night. So an elapsed
    backoff that classifies dark re-arms instead of reconnecting; only a non-dark
    wake/return ever breaks a sleeping backoff. An awake outage has no DarkWakes,
    so its backoff drains uninterrupted and reconnects normally.

    Logs are retrospective, never a countdown: the backoff delay is event-loop
    time that freezes during sleep and is almost always cut short by a wake, so a
    "retry in Xs" line would promise a moment that never arrives. Instead it marks
    the *decision* — ``disconnected; waiting to reconnect`` on entering an awake
    backoff, ``dark wake; still offline (maintenance wake)`` when a wake classifies
    dark — and the *act* — ``reconnecting now (...)`` right before each real
    attempt, with the reason. The backoff tier itself stays in ``ReconnectState``.

    The dark-wake line is logged once per *wake*, not once per re-arm: one long
    maintenance DarkWake keeps the CPU up long enough for the (jittered) backoff
    to drain and re-arm several times, each re-confirming dark, which would print
    the identical line 4-5 times. ``dark_announced`` suppresses those drain
    repeats; a fresh ``retry_now`` (a genuinely new wake) always re-announces.

    Returns ``True`` to reconnect early (caller resets the ladder), ``False`` if
    the full delay elapsed, or ``None`` if cancelled.
    """
    remaining = delay
    pending = retry_now.is_set()
    if not pending:
        logger.info("disconnected; waiting to reconnect")
    # Set while the current dark stretch has already been announced, so the
    # backoff's repeated drain/re-arm within one DarkWake stays quiet.
    dark_announced = False
    while True:
        if pending:
            pending = False
            retry_now.clear()
            try:
                dark = await is_dark_wake()
            except asyncio.CancelledError:
                return None
            if not dark:
                logger.info("reconnecting now (wake/network return)")
                return True
            # A genuine wake signal — always announce, even if a prior drain
            # already did, so each wake pairs with one line.
            logger.info("dark wake; still offline (maintenance wake)")
            dark_announced = True
            # A DarkWake's brief CPU still advances `remaining`, but the machine
            # is asleep — re-arm the full delay so the backoff can't expire
            # mid-sleep and reconnect. Only the non-dark branch above leaves.
            remaining = delay
        if remaining <= 0:
            # Backoff drained with no wake interrupt. Reconnect only if we're
            # genuinely awake; a drain that accumulated across silent DarkWakes
            # is still a sleeping machine, so re-arm and keep waiting.
            try:
                dark = await is_dark_wake()
            except asyncio.CancelledError:
                return None
            if dark:
                if not dark_announced:
                    logger.info("dark wake; still offline (maintenance wake)")
                    dark_announced = True
                remaining = delay
                continue
            logger.info("reconnecting now (backoff elapsed)")
            return False
        start = time.monotonic()
        interrupted = await _sleep_or_retry(remaining, retry_now)
        if interrupted is None:
            return None
        if not interrupted:
            remaining = 0
            continue
        remaining -= time.monotonic() - start
        pending = True


async def _sleep_or_retry(delay: float, retry_now: asyncio.Event) -> bool | None:
    """Wait up to ``delay`` seconds, or until ``retry_now`` fires.

    Returns ``True`` if interrupted by the event, ``False`` if the full delay
    elapsed, or ``None`` if cancelled. Clears the event before returning so a
    single signal triggers exactly one early retry.
    """
    waiter = asyncio.create_task(retry_now.wait())
    sleeper = asyncio.create_task(asyncio.sleep(delay))
    try:
        done, _ = await asyncio.wait(
            {waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED
        )
    except asyncio.CancelledError:
        return None
    finally:
        waiter.cancel()
        sleeper.cancel()
        with contextlib.suppress(BaseException):
            await waiter
        with contextlib.suppress(BaseException):
            await sleeper
        retry_now.clear()
    return waiter in done


async def _send_offline_notice(
    orchestrator: Orchestrator, offline_seconds: float
) -> None:
    """Tell the phone the daemon was offline and inbound may have been dropped.

    Swallows transport failures the same way ``_auto_update_check`` does: this
    runs as a fire-and-forget task off the reconnect path, and a failed push
    must not escape into the loop and kill it.
    """
    try:
        await orchestrator.notify(
            f"⚠️ Reconnected after ~{format_duration(int(offline_seconds))} offline."
        )
    except Exception:  # noqa: BLE001 - a bad notice must not kill the loop
        logger.warning("offline-recovery notify failed", exc_info=True)


async def _auto_update_check(orchestrator: Orchestrator) -> None:
    """One fetch+compare against origin/main; nudge the phone only if behind.

    Silent when up to date and silent on error — a failed check (e.g. an
    offline network or an SSH-auth hiccup under launchd's minimal env) is
    logged, never pushed to the phone, so it can't turn into daily noise.
    """
    try:
        status = await self_update.fetch_and_compare()
    except Exception:  # noqa: BLE001 - a bad check must stay silent on the phone
        logger.warning("auto update check failed", exc_info=True)
        return
    if status.behind:
        # The notify send can blip on a transient transport failure; let it
        # stay silent like the fetch errors above rather than escape into
        # _auto_update_loop's while-True and kill the loop until next restart.
        try:
            await orchestrator.notify(
                "🔔 An update for claude-dingtalk-bridge is available — "
                "send `/update` to apply."
            )
        except Exception:  # noqa: BLE001 - a bad nudge must not kill the loop
            logger.warning("auto update notify failed", exc_info=True)


async def _auto_update_loop(orchestrator: Orchestrator) -> None:
    """Check for updates once after an initial delay, then every 24 hours."""
    await asyncio.sleep(_AUTO_UPDATE_INITIAL_DELAY)
    while True:
        await _auto_update_check(orchestrator)
        await asyncio.sleep(_AUTO_UPDATE_CHECK_INTERVAL)


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

    retry_now = asyncio.Event()
    disconnected = asyncio.Event()

    # asyncio keeps only a weak reference to a bare create_task, so a
    # fire-and-forget notice can be garbage-collected mid-flight and never reach
    # the phone. Hold a strong reference until each one finishes.
    notice_tasks: set[asyncio.Task] = set()

    def _spawn_offline_notice(seconds: float) -> None:
        task = asyncio.create_task(_send_offline_notice(orchestrator, seconds))
        notice_tasks.add(task)
        task.add_done_callback(notice_tasks.discard)

    client_task = asyncio.create_task(
        _drive_stream_client(
            client,
            retry_now=retry_now,
            disconnected=disconnected,
            on_recovered=_spawn_offline_notice,
        )
    )
    # Both watchers are pure backoff-interrupters gated on `disconnected`: they
    # nudge `retry_now` only while the loop is down, never while connected.
    wake_task = asyncio.create_task(
        watch_wake(is_disconnected=disconnected.is_set, on_wake=retry_now.set)
    )
    reach_task = asyncio.create_task(
        watch_reachability(
            is_disconnected=disconnected.is_set, on_recover=retry_now.set
        )
    )
    auto_update_task = asyncio.create_task(_auto_update_loop(orchestrator))
    stop_wait = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait(
            {client_task, stop_wait}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for extra in (stop_wait, auto_update_task, wake_task, reach_task):
            extra.cancel()
            with contextlib.suppress(BaseException):
                await extra
        # Stop the stream driver before draining notices: a reconnect during
        # shutdown spawns an offline notice from `_on_connect`, and one landing
        # after the drain's snapshot below would never be awaited. Cancelling
        # the driver first freezes `notice_tasks` so the drain sees every one.
        client_task.cancel()
        with contextlib.suppress(BaseException):
            await client_task
        # Let an in-flight offline-recovery notice finish its send rather than
        # dropping the "messages may have been lost" hint mid-flight; bound the
        # wait so a hung transport can't stall exit, then cancel any straggler.
        if notice_tasks:
            with contextlib.suppress(BaseException):
                _, pending = await asyncio.wait(
                    set(notice_tasks), timeout=_SHUTDOWN_NOTICE_TIMEOUT
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
        try:
            await orchestrator.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("Orchestrator shutdown raised")
