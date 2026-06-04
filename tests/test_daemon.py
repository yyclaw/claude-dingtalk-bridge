import asyncio
import contextlib

import websockets

from claude_dingtalk_bridge.config import Config, GeoConfig, Project
from claude_dingtalk_bridge.daemon import (
    _ChatHandler,
    _disable_websocket_proxy,
    _mask_sender,
    _filter_client_noise,
    build_image_prompt,
    build_orchestrator,
)
from claude_dingtalk_bridge.orchestrator import Orchestrator


def make_config() -> Config:
    return Config(
        dingtalk_client_id="k",
        dingtalk_client_secret="s",
        authorized_user_id="staff-1",
        projects=[Project(name="p", path="/tmp/p")],
    )


def make_geo_config() -> Config:
    base = make_config()
    base.geo = GeoConfig(proxy_url="http://127.0.0.1:7777", target_country="US")
    return base


def test_build_orchestrator_without_geo_leaves_proxy_none():
    orchestrator, _ = build_orchestrator(make_config())
    assert orchestrator._geo_check is None
    assert orchestrator._runner.proxy_url is None


def test_build_orchestrator_with_geo_wires_proxy_and_check():
    orchestrator, _ = build_orchestrator(make_geo_config())
    assert orchestrator._geo_check is not None
    assert orchestrator._runner.proxy_url == "http://127.0.0.1:7777"


def test_build_orchestrator_returns_orchestrator():
    orchestrator, transport = build_orchestrator(make_config())
    assert isinstance(orchestrator, Orchestrator)


def test_build_orchestrator_wires_permission_handler():
    orchestrator, transport = build_orchestrator(make_config())
    assert orchestrator._runner.permission_handler == orchestrator.request_permission


async def test_build_orchestrator_markdown_sender_uses_markdown_template(monkeypatch):
    orchestrator, transport = build_orchestrator(make_config())
    calls: list = []
    monkeypatch.setattr(
        transport, "send_markdown",
        lambda uid, title, text: calls.append(("md", uid, title, text)),
    )
    monkeypatch.setattr(
        transport, "send_text", lambda uid, text: calls.append(("text", uid, text)),
    )
    await orchestrator._send_markdown("plain body without a heading")
    assert calls == [
        ("md", "staff-1", "Claude has replied.", "plain body without a heading")
    ]


async def test_markdown_sender_splits_oversized_body(monkeypatch):
    import claude_dingtalk_bridge.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "_MARKDOWN_BYTE_BUDGET", 10)
    orchestrator, transport = build_orchestrator(make_config())
    calls: list = []
    monkeypatch.setattr(
        transport, "send_markdown",
        lambda uid, title, text: calls.append(text),
    )
    await orchestrator._send_markdown("AAAA\n\nBBBB\n\nCCCC\n\nDDDD")
    assert len(calls) > 1
    assert all(len(t.encode("utf-8")) <= 10 for t in calls)


async def test_markdown_sender_splits_by_line_budget(monkeypatch):
    import claude_dingtalk_bridge.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "_MARKDOWN_BYTE_BUDGET", 100_000)
    monkeypatch.setattr(daemon_mod, "_MARKDOWN_LINE_BUDGET", 3)
    orchestrator, transport = build_orchestrator(make_config())
    calls: list = []
    monkeypatch.setattr(
        transport, "send_markdown",
        lambda uid, title, text: calls.append(text),
    )
    await orchestrator._send_markdown("\n".join(f"L{n}" for n in range(9)))
    assert len(calls) > 1
    assert all(t.count("\n") + 1 <= 3 for t in calls)


async def test_markdown_sender_pads_long_code_chunk_tail(monkeypatch):
    import claude_dingtalk_bridge.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "_CODE_TAIL_MIN_LINES", 35)
    monkeypatch.setattr(daemon_mod, "_CODE_TAIL_MARGIN", 2)
    orchestrator, transport = build_orchestrator(make_config())
    calls: list = []
    monkeypatch.setattr(
        transport, "send_markdown",
        lambda uid, title, text: calls.append(text),
    )
    code = "\n".join(f"c{n:02d}" for n in range(40))
    await orchestrator._send_markdown(f"```python\n{code}\n```")
    assert len(calls) == 1
    lines = calls[0].split("\n")
    assert lines[-1] == "```"
    assert lines[-4:-1] == ["", "", ""]  # 3 sacrificial blanks (est 1 + margin 2)
    assert lines[-5] == "c39"            # real code ends just above the padding


async def test_build_orchestrator_markdown_sender_lifts_heading_as_title(monkeypatch):
    orchestrator, transport = build_orchestrator(make_config())
    calls: list = []
    monkeypatch.setattr(
        transport, "send_markdown",
        lambda uid, title, text: calls.append((uid, title, text)),
    )
    body = "### 🔐 Permission needed\n\nBash · ls"
    await orchestrator._send_markdown(body)
    assert calls == [("staff-1", "🔐 Permission needed", body)]


class _FakeCallback:
    def __init__(self, data):
        self.data = data


class _RecordingOrchestrator:
    def __init__(self):
        self.messages: list = []
        self.audios: list = []
        self.images: list = []
        self.notices: list = []

    def is_authorized(self, sender_id):
        return sender_id == "staff-1"

    async def handle_message(self, text, sender_id):
        self.messages.append((text, sender_id))

    async def handle_audio(self, recognition, sender_id):
        self.audios.append((recognition, sender_id))

    async def handle_image(self, prompt, sender_id):
        self.images.append((prompt, sender_id))

    async def notify(self, message):
        self.notices.append(message)


async def test_chat_handler_routes_audio_to_handle_audio():
    orch = _RecordingOrchestrator()
    handler = _ChatHandler(orch)
    await handler.process(_FakeCallback({
        "msgtype": "audio",
        "senderStaffId": "staff-1",
        "content": {"recognition": "fix the bug", "downloadCode": "x"},
    }))
    assert orch.audios == [("fix the bug", "staff-1")]
    assert orch.messages == []


async def test_chat_handler_audio_without_recognition_passes_none():
    orch = _RecordingOrchestrator()
    handler = _ChatHandler(orch)
    await handler.process(_FakeCallback({
        "msgtype": "audio",
        "senderStaffId": "staff-1",
        "content": {"downloadCode": "x"},
    }))
    assert orch.audios == [(None, "staff-1")]


async def test_chat_handler_picture_downloads_and_builds_single_image_prompt():
    orch = _RecordingOrchestrator()
    handler = _ChatHandler(orch)
    handler._fetch_image = lambda code: f"/tmp/img/{code}.png"
    await handler.process(_FakeCallback({
        "msgtype": "picture",
        "senderStaffId": "staff-1",
        "content": {"downloadCode": "abc"},
    }))
    assert len(orch.images) == 1
    prompt, sender = orch.images[0]
    assert sender == "staff-1"
    assert "I sent you an image:" in prompt
    assert "[image saved at /tmp/img/abc.png]" in prompt
    assert orch.messages == []


async def test_chat_handler_richtext_builds_interleaved_prompt():
    orch = _RecordingOrchestrator()
    handler = _ChatHandler(orch)
    handler._fetch_image = lambda code: f"/tmp/img/{code}.png"
    await handler.process(_FakeCallback({
        "msgtype": "richText",
        "senderStaffId": "staff-1",
        "content": {"richText": [
            {"text": "compare this"},
            {"downloadCode": "a"},
            {"text": "with"},
            {"downloadCode": "b"},
        ]},
    }))
    prompt, sender = orch.images[0]
    assert sender == "staff-1"
    assert "text and images" in prompt
    assert (
        "compare this [image saved at /tmp/img/a.png] with "
        "[image saved at /tmp/img/b.png]"
    ) in prompt


async def test_chat_handler_notifies_phone_when_image_download_fails():
    orch = _RecordingOrchestrator()
    handler = _ChatHandler(orch)

    def boom(code):
        raise RuntimeError("network down")

    handler._fetch_image = boom
    await handler.process(_FakeCallback({
        "msgtype": "picture",
        "senderStaffId": "staff-1",
        "content": {"downloadCode": "abc"},
    }))
    # No image dispatched, but the user gets an explicit error rather than
    # silently dropped messages.
    assert orch.images == []
    assert len(orch.notices) == 1
    assert "Couldn't download" in orch.notices[0]
    assert "network down" in orch.notices[0]


async def test_chat_handler_rejects_image_from_unauthorized_sender():
    orch = _RecordingOrchestrator()
    handler = _ChatHandler(orch)
    fetched: list = []
    handler._fetch_image = lambda code: fetched.append(code) or f"/tmp/{code}.png"
    await handler.process(_FakeCallback({
        "msgtype": "picture",
        "senderStaffId": "intruder",
        "content": {"downloadCode": "abc"},
    }))
    # Turned away before any download — no fetch, nothing handed to the orchestrator.
    assert fetched == []
    assert orch.images == []


def test_build_image_prompt_single_image_no_text():
    prompt = build_image_prompt([("image", "/tmp/a.png")])
    assert prompt == (
        "I sent you an image: [image saved at /tmp/a.png]. Please take a look."
    )


def test_build_image_prompt_interleaves_text_and_images():
    prompt = build_image_prompt([
        ("text", "look at"),
        ("image", "/tmp/a.png"),
    ])
    assert prompt == (
        "I sent you a message with text and images:\n\n"
        "look at [image saved at /tmp/a.png]"
    )


def test_disable_websocket_proxy_forces_no_proxy(monkeypatch):
    calls: dict = {}

    def fake_connect(*args, **kwargs):
        calls["kwargs"] = kwargs

    monkeypatch.setattr(websockets, "connect", fake_connect)
    _disable_websocket_proxy()
    websockets.connect("wss://example.com")

    assert calls["kwargs"]["proxy"] is None


def test_disable_websocket_proxy_keeps_explicit_proxy(monkeypatch):
    calls: dict = {}

    def fake_connect(*args, **kwargs):
        calls["kwargs"] = kwargs

    monkeypatch.setattr(websockets, "connect", fake_connect)
    _disable_websocket_proxy()
    websockets.connect("wss://example.com", proxy="http://p")

    assert calls["kwargs"]["proxy"] == "http://p"


# --- send channel / geo wiring -----------------------------------------

import claude_dingtalk_bridge.daemon as daemon  # noqa: E402


async def test_build_orchestrator_text_sender_uses_text_template(monkeypatch):
    orchestrator, transport = build_orchestrator(make_config())
    calls: list = []
    monkeypatch.setattr(
        transport, "send_text", lambda uid, text: calls.append((uid, text))
    )
    await orchestrator._send("hello")
    assert calls == [("staff-1", "hello")]


async def test_build_orchestrator_geo_check_invokes_cached_check(monkeypatch):
    from claude_dingtalk_bridge.geo import GeoCheck

    sentinel = GeoCheck(ok=True, detail="🌍 US")
    monkeypatch.setattr(daemon, "CachedGeoCheck", lambda cfg: (lambda: sentinel))
    orchestrator, _ = build_orchestrator(make_geo_config())
    assert await orchestrator._geo_check() is sentinel


# --- _ChatHandler routing ----------------------------------------------

async def test_chat_handler_routes_text_to_handle_message():
    orch = _RecordingOrchestrator()
    handler = _ChatHandler(orch)
    await handler.process(_FakeCallback({
        "msgtype": "text",
        "senderStaffId": "staff-1",
        "text": {"content": "  fix the bug  "},
    }))
    assert orch.messages == [("fix the bug", "staff-1")]


async def test_chat_handler_swallows_handler_exceptions():
    class _Boom:
        def is_authorized(self, sender_id):
            return True

        async def handle_message(self, text, sender_id):
            raise RuntimeError("kaboom")

    handler = _ChatHandler(_Boom())
    status, body = await handler.process(_FakeCallback({
        "msgtype": "text",
        "senderStaffId": "staff-1",
        "text": {"content": "hi"},
    }))
    # One bad message must never kill the loop — process still acks.
    assert body == "OK"


async def test_chat_handler_richtext_without_image_falls_back_to_text():
    # A richText made of text items only (a sticker / emoji bundled with text,
    # an @-mention without payload, …) used to be silently dropped. Salvage
    # the text so the user's intent isn't lost.
    orch = _RecordingOrchestrator()
    handler = _ChatHandler(orch)
    await handler.process(_FakeCallback({
        "msgtype": "richText",
        "senderStaffId": "staff-1",
        "content": {"richText": [
            {"text": "just"},
            {"text": " words"},
            {"type": "at", "atUserId": "u1"},
        ]},
    }))
    assert orch.images == []
    assert orch.messages == [("just words", "staff-1")]


async def test_chat_handler_richtext_with_no_text_no_image_is_silent():
    # richText with only @-mentions / unknown items (no text, no image) has
    # nothing to salvage — silently dropped is the right answer here.
    orch = _RecordingOrchestrator()
    handler = _ChatHandler(orch)
    await handler.process(_FakeCallback({
        "msgtype": "richText",
        "senderStaffId": "staff-1",
        "content": {"richText": [{"type": "at", "atUserId": "u1"}]},
    }))
    assert orch.messages == [] and orch.images == []


async def test_chat_handler_richtext_text_only_unauthorized_is_dropped():
    # Auth check still gates the text fallback — an unauthorized sender's
    # text from a richText must not reach handle_message.
    orch = _RecordingOrchestrator()
    handler = _ChatHandler(orch)
    await handler.process(_FakeCallback({
        "msgtype": "richText",
        "senderStaffId": "stranger",
        "content": {"richText": [{"text": "hi"}]},
    }))
    assert orch.messages == [] and orch.images == []


def test_fetch_image_resolves_download_code(monkeypatch):
    from pathlib import Path

    handler = _ChatHandler(_RecordingOrchestrator())
    handler.get_image_download_url = lambda code: f"http://dl/{code}"
    monkeypatch.setattr(daemon, "download_image", lambda url: Path("/tmp/img.png"))
    assert handler._fetch_image("abc") == "/tmp/img.png"


# --- run() --------------------------------------------------------------

class _FakeStreamClient:
    instances: list = []

    def __init__(self, credential):
        self.credential = credential
        self.handlers: list = []
        self.raise_keyboard = False
        self.start_calls = 0
        _FakeStreamClient.instances.append(self)

    def register_callback_handler(self, topic, handler):
        self.handlers.append((topic, handler))

    async def start(self):
        self.start_calls += 1
        if self.raise_keyboard:
            raise KeyboardInterrupt
        # Block until cancelled — _serve will cancel us on shutdown.
        await asyncio.Event().wait()


def _patch_run(monkeypatch, keyboard=False):
    monkeypatch.setattr(daemon, "load_config", make_config)
    monkeypatch.setattr(daemon, "_disable_websocket_proxy", lambda: None)
    _FakeStreamClient.instances = []

    class _Client(_FakeStreamClient):
        def __init__(self, credential):
            super().__init__(credential)
            self.raise_keyboard = keyboard

    monkeypatch.setattr(daemon.dingtalk_stream, "DingTalkStreamClient", _Client)



def test_run_registers_handler_then_shuts_down_on_signal(monkeypatch):
    _patch_run(monkeypatch)

    # Drive shutdown by directly awaiting orchestrator.shutdown via a stub:
    # patch _serve to register the handler check then return immediately.
    async def fake_serve(client, orchestrator):
        # Stream wiring already happened in run(); this stub stands in for
        # the "received SIGTERM, exit cleanly" path.
        return

    monkeypatch.setattr(daemon, "_serve", fake_serve)
    daemon.run()
    client = _FakeStreamClient.instances[-1]
    assert len(client.handlers) == 1


def test_run_swallows_keyboard_interrupt(monkeypatch):
    # When client.start() raises KeyboardInterrupt, _drive_stream_client logs
    # and retries; on the second iteration it raises again. asyncio.run
    # eventually surfaces it, and run() swallows it. We just need to verify
    # the daemon doesn't propagate.
    _patch_run(monkeypatch, keyboard=True)

    async def fake_serve(client, orchestrator):
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "_serve", fake_serve)
    daemon.run()


# --- _serve / shutdown -------------------------------------------------


class _StubOrchestrator:
    def __init__(self, shutdown_raises=False):
        self.shutdown_called = 0
        self.notices: list = []
        self.shutdown_raises = shutdown_raises

    async def shutdown(self):
        self.shutdown_called += 1
        if self.shutdown_raises:
            raise RuntimeError("shutdown blew up")

    async def notify(self, message):
        # The daemon shouldn't be calling notify on lifecycle events any more --
        # those moved to the CLI. Record anything that slips through so the
        # tests below can assert it stays empty.
        self.notices.append(message)


async def test_serve_stops_on_signal_event_runs_shutdown():
    client = _FakeStreamClient(credential=None)
    orch = _StubOrchestrator()
    serve_task = asyncio.create_task(daemon._serve(client, orch))
    await asyncio.sleep(0)  # let _serve register and start client
    # Simulate the SIGTERM handler firing by directly setting the stop event
    # of _serve's running loop — _serve adds it via add_signal_handler, but
    # tests can race the same effect by raising KeyboardInterrupt-like cancel.
    serve_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await serve_task
    assert orch.shutdown_called == 1
    # Lifecycle notifications now live in the CLI -- the daemon stays silent
    # on graceful shutdown so a restart doesn't surface as a misleading "stop".
    assert orch.notices == []


async def test_serve_survives_shutdown_failure():
    client = _FakeStreamClient(credential=None)
    orch = _StubOrchestrator(shutdown_raises=True)
    serve_task = asyncio.create_task(daemon._serve(client, orch))
    await asyncio.sleep(0)
    serve_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await serve_task
    # shutdown() raised, but _serve swallowed the exception, finished its
    # cleanup, and returned without crashing the process.
    assert orch.shutdown_called == 1


def test_filter_client_noise_drops_and_downgrades():
    import logging as _logging

    _filter_client_noise()
    sdk_logger = _logging.getLogger("dingtalk_stream.client")
    captured: list[tuple[int, str]] = []

    class _Capture(_logging.Handler):
        def emit(self, record):
            captured.append((record.levelno, record.getMessage()))

    handler = _Capture(level=_logging.DEBUG)
    sdk_logger.addHandler(handler)
    sdk_logger.setLevel(_logging.DEBUG)
    try:
        # Shutdown path: CancelledError is the first arg -- dropped regardless
        # of the format string the SDK happens to use.
        sdk_logger.error("[start] network exception, error=%s", asyncio.CancelledError())
        sdk_logger.error("anything at all, error=%s", asyncio.CancelledError())
        # Auto-reconnect events: kept but downgraded ERROR -> WARNING so they
        # stay in err.log (where outage signals belong) and out of out.log.
        sdk_logger.error(
            "[start] network exception, error=%s",
            ConnectionResetError("peer reset"),
        )
        sdk_logger.error("open connection failed")
        # Unrelated ERROR: passes through untouched.
        sdk_logger.error("token refresh failed")
    finally:
        sdk_logger.removeHandler(handler)
    assert captured == [
        (_logging.WARNING, "[start] network exception, error=peer reset"),
        (_logging.WARNING, "open connection failed"),
        (_logging.ERROR, "token refresh failed"),
    ]


async def test_drive_stream_client_reconnects_on_failure(monkeypatch):
    # The drive loop must call _serve_stream_once again after a failed attempt
    # — a transient gateway hiccup mustn't terminate the daemon.
    calls = 0

    async def fake_serve_once(client):
        nonlocal calls
        calls += 1
        if calls < 3:
            return None  # connection never came up
        raise asyncio.CancelledError  # break the loop on the 3rd attempt

    monkeypatch.setattr(daemon, "_serve_stream_once", fake_serve_once)
    state = daemon.ReconnectState(delays=(0.0,), jitter=False)
    with contextlib.suppress(asyncio.CancelledError):
        await daemon._drive_stream_client(object(), state=state)
    assert calls == 3


async def test_chat_handler_unknown_message_type_notifies_user(caplog):
    # A message type the handler has no branch for (file, link, sticker, …)
    # used to be silently dropped. Surface it to the phone AND log it so the
    # next "I sent a message but nothing happened" leaves a trail.
    import logging
    orch = _RecordingOrchestrator()
    handler = _ChatHandler(orch)
    with caplog.at_level(logging.WARNING, logger="claude_dingtalk_bridge.daemon"):
        await handler.process(_FakeCallback({
            "msgtype": "file",
            "senderStaffId": "staff-1",
        }))
    assert orch.messages == []
    assert orch.audios == []
    assert orch.images == []
    assert len(orch.notices) == 1 and "file" in orch.notices[0]
    assert any("unsupported msgtype" in r.getMessage() and "file" in r.getMessage()
               for r in caplog.records)


async def test_chat_handler_unknown_message_type_unauthorized_is_silent(caplog):
    # An unauthorized sender's unknown msgtype must NOT trigger a phone
    # notify (which would leak to whoever is testing the bot from outside).
    import logging
    orch = _RecordingOrchestrator()
    handler = _ChatHandler(orch)
    with caplog.at_level(logging.WARNING, logger="claude_dingtalk_bridge.daemon"):
        await handler.process(_FakeCallback({
            "msgtype": "file",
            "senderStaffId": "stranger",
        }))
    assert orch.notices == []


async def test_chat_handler_logs_every_inbound_message(caplog):
    # The single most important debug aid: every callback that reaches
    # process() must leave a log line, so a "I sent a message and the daemon
    # did nothing" report can immediately confirm where the silence started.
    import logging
    orch = _RecordingOrchestrator()
    handler = _ChatHandler(orch)
    with caplog.at_level(logging.INFO, logger="claude_dingtalk_bridge.daemon"):
        await handler.process(_FakeCallback({
            "msgtype": "text",
            "senderStaffId": "staff-1",
            "text": {"content": "hello"},
        }))
        await handler.process(_FakeCallback({
            "msgtype": "audio",
            "senderStaffId": "staff-1",
            "content": {"recognition": "fix bug", "downloadCode": "x"},
        }))
        await handler.process(_FakeCallback({
            "msgtype": "file",
            "senderStaffId": "staff-1",
        }))
    inbound = [r.getMessage() for r in caplog.records
               if r.getMessage().startswith("inbound ")]
    assert len(inbound) == 3
    # Sender id is masked to first-2 + *** + last-2; raw value must not leak.
    masked = "st***-1"
    assert all("staff-1" not in m for m in inbound)
    assert any("msgtype=text" in m and masked in m for m in inbound)
    assert any("msgtype=audio" in m and masked in m for m in inbound)
    assert any("msgtype=file" in m and masked in m for m in inbound)


def test_mask_sender_masks_long_value_and_handles_empty():
    # Normal-length staff id: first-2 + *** + last-2.
    assert _mask_sender("staff-12345") == "st***45"
    # Empty / missing sender falls back to "?" so the log line stays parseable.
    assert _mask_sender("") == "?"
    assert _mask_sender(None) == "?"


def test_mask_sender_short_id_fully_masked():
    # Ids of 4 chars or fewer can't be masked by first-2 + last-2 without
    # exposing (or duplicating) the whole value, so they're masked entirely.
    assert _mask_sender("abc") == "***"    # 3 chars: slices would overlap
    assert _mask_sender("abcd") == "***"   # 4 chars: slices would expose all
    assert _mask_sender("abcde") == "ab***de"  # 5 chars: 'c' stays hidden


async def test_chat_handler_richtext_drops_non_text_non_image_items():
    # richText items that are neither text nor image (@-mentions, links) are
    # skipped while the surrounding images still come through.
    orch = _RecordingOrchestrator()
    handler = _ChatHandler(orch)
    handler._fetch_image = lambda code: f"/tmp/img/{code}.png"
    await handler.process(_FakeCallback({
        "msgtype": "richText",
        "senderStaffId": "staff-1",
        "content": {"richText": [
            {"type": "at", "atUserId": "u1"},
            {"downloadCode": "a"},
        ]},
    }))
    prompt, sender = orch.images[0]
    assert sender == "staff-1"
    assert "[image saved at /tmp/img/a.png]" in prompt


def test_run_tolerates_stream_reconfigure_failures(monkeypatch):
    # pytest captures stdout/stderr as wrappers that may not support
    # reconfigure() (or may be closed). run() must tolerate either AttributeError
    # or OSError rather than crashing the daemon at boot.
    import sys

    _patch_run(monkeypatch)

    class _BadStream:
        def reconfigure(self, **_kwargs):
            raise OSError("not a TextIOWrapper")

        # Need write/flush so logging handlers attached later don't crash.
        def write(self, *_a, **_k):
            return 0

        def flush(self):
            pass

        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdout", _BadStream())
    monkeypatch.setattr(sys, "stderr", _BadStream())

    async def fake_serve(client, orchestrator):
        return

    monkeypatch.setattr(daemon, "_serve", fake_serve)
    daemon.run()  # must not raise


async def test_drive_stream_client_returns_on_cancel_during_reconnect_sleep(
    monkeypatch,
):
    # The reconnect loop sleeps between attempts; cancelling the task while
    # the sleep is in flight must exit cleanly (no warning, no re-raise).
    # Use a non-zero delay so the cancel reliably lands on the sleep, not on
    # _serve_stream_once.
    async def fake_serve_once(client):
        return None  # immediate failure → enter sleep

    monkeypatch.setattr(daemon, "_serve_stream_once", fake_serve_once)
    state = daemon.ReconnectState(delays=(5.0,), jitter=False)

    task = asyncio.create_task(
        daemon._drive_stream_client(object(), state=state)
    )
    # Yield enough times for _serve_stream_once to fail and the sleep to begin.
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    # The handler swallows CancelledError and returns normally — no exception
    # should propagate out of `await task`.
    await task


async def test_serve_tolerates_missing_signal_handler_support(monkeypatch):
    # Some loops (Windows, sub-thread loops, etc.) reject add_signal_handler
    # with NotImplementedError / RuntimeError. _serve must fall through to
    # default signal semantics rather than crashing at startup.
    class _Loop:
        def add_signal_handler(self, sig, cb):
            raise NotImplementedError("not supported on this loop")

    real_get = asyncio.get_running_loop

    def fake_get_loop():
        # The real loop is needed for the rest of _serve (Event, create_task);
        # return a wrapper that only overrides add_signal_handler.
        real = real_get()

        class _Wrapper:
            def __getattr__(self, name):
                if name == "add_signal_handler":
                    raise NotImplementedError("not supported")
                return getattr(real, name)

        return _Wrapper()

    monkeypatch.setattr(asyncio, "get_running_loop", fake_get_loop)
    client = _FakeStreamClient(credential=None)
    orch = _StubOrchestrator()
    serve_task = asyncio.create_task(daemon._serve(client, orch))
    await asyncio.sleep(0)
    serve_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await serve_task
    # _serve still wired the rest of the lifecycle even though signal handlers
    # couldn't be installed.
    assert orch.shutdown_called == 1


# --- _open_connection / _serve_stream_once ----------------------------


class _FakeOpenClient:
    """Minimal stand-in exposing what _open_connection reads off the SDK client."""

    def __init__(self, *, callbacks=(), event_required=False):
        self._is_event_required = event_required
        self.callback_handler_map = {topic: object() for topic in callbacks}

        class _Credential:
            client_id = "cid"
            client_secret = "csec"

        self.credential = _Credential()

    def get_host_ip(self):
        return "127.0.0.1"


def test_open_connection_posts_with_timeout_and_returns_json(monkeypatch):
    import json as _json

    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"endpoint": "wss://gw", "ticket": "tkt"}

    def fake_post(url, headers, data, timeout):
        captured["url"] = url
        captured["timeout"] = timeout
        captured["body"] = _json.loads(data)
        return _Resp()

    monkeypatch.setattr(daemon.requests, "post", fake_post)
    client = _FakeOpenClient(callbacks=["topic-a"], event_required=False)

    result = daemon._open_connection(client)

    assert result == {"endpoint": "wss://gw", "ticket": "tkt"}
    # The whole point of this wrapper: a real timeout, not the SDK's None.
    assert captured["timeout"] == daemon._OPEN_CONNECTION_TIMEOUT
    assert captured["body"]["clientId"] == "cid"
    assert captured["body"]["clientSecret"] == "csec"
    assert {"type": "CALLBACK", "topic": "topic-a"} in captured["body"]["subscriptions"]
    # No EVENT topic when the client doesn't ask for one.
    assert all(s["type"] != "EVENT" for s in captured["body"]["subscriptions"])


def test_open_connection_subscribes_to_event_topic_when_required(monkeypatch):
    import json as _json

    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"endpoint": "wss://gw", "ticket": "tkt"}

    def fake_post(url, headers, data, timeout):
        captured["body"] = _json.loads(data)
        return _Resp()

    monkeypatch.setattr(daemon.requests, "post", fake_post)
    client = _FakeOpenClient(callbacks=[], event_required=True)

    daemon._open_connection(client)

    assert {"type": "EVENT", "topic": "*"} in captured["body"]["subscriptions"]


def test_open_connection_returns_none_and_warns_on_request_error(monkeypatch, caplog):
    import logging

    def fake_post(*_a, **_kw):
        raise daemon.requests.exceptions.ConnectTimeout("connect timeout")

    monkeypatch.setattr(daemon.requests, "post", fake_post)
    client = _FakeOpenClient(callbacks=["x"], event_required=False)

    with caplog.at_level(logging.WARNING, logger="claude_dingtalk_bridge.daemon"):
        result = daemon._open_connection(client)

    assert result is None
    assert any(
        "open connection failed" in r.getMessage() for r in caplog.records
    )


def test_open_connection_returns_none_on_http_error(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            raise daemon.requests.exceptions.HTTPError("500 server error")

        def json(self):  # pragma: no cover - should not be reached
            return {}

    monkeypatch.setattr(
        daemon.requests, "post", lambda url, headers, data, timeout: _Resp()
    )
    client = _FakeOpenClient(callbacks=["x"], event_required=False)

    assert daemon._open_connection(client) is None


class _FakeServeClient:
    """Stand-in covering the surface _serve_stream_once touches on the SDK client."""

    def __init__(self):
        self.pre_start_called = False
        self.background_payloads: list = []
        self.keepalive_started = False
        self.keepalive_cancelled = False
        self.websocket = None

    def pre_start(self):
        self.pre_start_called = True

    async def keepalive(self, _ws):
        self.keepalive_started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.keepalive_cancelled = True
            raise

    async def background_task(self, payload):
        self.background_payloads.append(payload)


class _FakeWebsocket:
    def __init__(self, messages):
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Yield so background tasks scheduled by the loop can interleave.
        await asyncio.sleep(0)
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _FakeConnectCM:
    def __init__(self, ws_or_exc):
        self._ws_or_exc = ws_or_exc

    async def __aenter__(self):
        if isinstance(self._ws_or_exc, BaseException):
            raise self._ws_or_exc
        return self._ws_or_exc

    async def __aexit__(self, *_exc):
        return False


async def test_serve_stream_once_returns_none_when_open_connection_fails(monkeypatch):
    monkeypatch.setattr(daemon, "_open_connection", lambda _client: None)
    client = _FakeServeClient()

    result = await daemon._serve_stream_once(client)

    assert result is None
    # pre_start still runs so SDK-internal token refresh / handler binding
    # happens even on a failed cycle.
    assert client.pre_start_called
    # Never reached the websocket leg, so keepalive must not have started.
    assert client.keepalive_started is False


async def test_serve_stream_once_iterates_messages_and_returns_duration(monkeypatch):
    import json as _json

    monkeypatch.setattr(
        daemon,
        "_open_connection",
        lambda _client: {"endpoint": "wss://gw", "ticket": "t/k+v"},
    )

    captured_uri: list[str] = []

    def fake_connect(uri):
        captured_uri.append(uri)
        return _FakeConnectCM(
            _FakeWebsocket([_json.dumps({"a": 1}), _json.dumps({"b": 2})])
        )

    monkeypatch.setattr(daemon.websockets, "connect", fake_connect)
    client = _FakeServeClient()

    result = await daemon._serve_stream_once(client)
    # Background tasks are scheduled, not awaited, by _serve_stream_once;
    # yield once so the assertions see them complete.
    await asyncio.sleep(0)

    assert result is not None and result >= 0
    # The ticket is URL-quoted so '/' and '+' survive the round trip.
    assert "ticket=t%2Fk%2Bv" in captured_uri[0]
    assert captured_uri[0].startswith("wss://gw?ticket=")
    assert client.keepalive_started
    assert client.keepalive_cancelled
    assert client.websocket is not None
    assert client.background_payloads == [{"a": 1}, {"b": 2}]


async def test_serve_stream_once_returns_none_when_handshake_raises(monkeypatch):
    # websockets.connect() raising before __aenter__ returns means opened_at
    # stays None — the function must report "never came up", not a duration of 0.
    monkeypatch.setattr(
        daemon,
        "_open_connection",
        lambda _client: {"endpoint": "wss://gw", "ticket": "t"},
    )
    monkeypatch.setattr(
        daemon.websockets,
        "connect",
        lambda _uri: _FakeConnectCM(OSError("handshake failed")),
    )
    client = _FakeServeClient()

    result = await daemon._serve_stream_once(client)

    assert result is None
    assert client.keepalive_started is False


async def test_serve_stream_once_swallows_mid_stream_error_and_returns_duration(
    monkeypatch, caplog
):
    import logging

    monkeypatch.setattr(
        daemon,
        "_open_connection",
        lambda _client: {"endpoint": "wss://gw", "ticket": "t"},
    )

    class _ErrorWS:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("ws went bad")

    monkeypatch.setattr(
        daemon.websockets,
        "connect",
        lambda _uri: _FakeConnectCM(_ErrorWS()),
    )
    client = _FakeServeClient()

    with caplog.at_level(logging.WARNING, logger="claude_dingtalk_bridge.daemon"):
        result = await daemon._serve_stream_once(client)

    # opened_at was set inside __aenter__ before the iterator raised, so the
    # function reports the (very short) live duration — telling the outer
    # backoff this was an "up then died" failure, not a never-connected one.
    assert result is not None and result >= 0
    assert any(
        "stream connection error" in r.getMessage() for r in caplog.records
    )


async def test_serve_stream_once_propagates_cancel(monkeypatch):
    monkeypatch.setattr(
        daemon,
        "_open_connection",
        lambda _client: {"endpoint": "wss://gw", "ticket": "t"},
    )

    class _BlockingWS:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        daemon.websockets,
        "connect",
        lambda _uri: _FakeConnectCM(_BlockingWS()),
    )
    client = _FakeServeClient()

    task = asyncio.create_task(daemon._serve_stream_once(client))
    # Yield enough times to enter the async-for and block on __anext__.
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    import pytest

    with pytest.raises(asyncio.CancelledError):
        await task
    # Keepalive was started and must have been cancelled in the finally block.
    assert client.keepalive_started
    assert client.keepalive_cancelled


# --- auto-update check -------------------------------------------------


async def test_auto_update_check_notifies_when_behind(monkeypatch):
    from claude_dingtalk_bridge import self_update

    async def fake_fetch(*_a, **_k):
        return self_update.CompareResult(behind=3, subjects=["x"])

    monkeypatch.setattr(daemon.self_update, "fetch_and_compare", fake_fetch)
    orch = _StubOrchestrator()
    await daemon._auto_update_check(orch)
    assert len(orch.notices) == 1
    assert "/update" in orch.notices[0]
    assert "claude-dingtalk-bridge" in orch.notices[0]


async def test_auto_update_check_silent_when_up_to_date(monkeypatch):
    from claude_dingtalk_bridge import self_update

    async def fake_fetch(*_a, **_k):
        return self_update.CompareResult(behind=0, subjects=[])

    monkeypatch.setattr(daemon.self_update, "fetch_and_compare", fake_fetch)
    orch = _StubOrchestrator()
    await daemon._auto_update_check(orch)
    assert orch.notices == []


async def test_auto_update_check_silent_on_error_but_logs(monkeypatch, caplog):
    import logging

    async def boom(*_a, **_k):
        raise daemon.self_update.SelfUpdateError("git fetch failed")

    monkeypatch.setattr(daemon.self_update, "fetch_and_compare", boom)
    orch = _StubOrchestrator()
    with caplog.at_level(logging.WARNING, logger="claude_dingtalk_bridge.daemon"):
        await daemon._auto_update_check(orch)
    # The check never pushes errors to the phone — only logs them.
    assert orch.notices == []
    assert any("auto update check" in r.getMessage().lower() for r in caplog.records)


async def test_serve_starts_and_cancels_auto_update_loop(monkeypatch):
    started = asyncio.Event()
    state = {"cancelled": False}

    async def fake_loop(orchestrator):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    monkeypatch.setattr(daemon, "_auto_update_loop", fake_loop)
    client = _FakeStreamClient(credential=None)
    orch = _StubOrchestrator()
    serve_task = asyncio.create_task(daemon._serve(client, orch))
    await started.wait()
    serve_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await serve_task
    # The loop is torn down with the rest of the daemon on shutdown.
    assert state["cancelled"]


async def test_auto_update_loop_waits_initial_delay_then_checks(monkeypatch):
    # The loop sleeps an initial delay, then runs a check; verify it calls the
    # check without waiting real time by stubbing sleep to break after one pass.
    checks = []

    async def fake_check(orchestrator):
        checks.append(orchestrator)

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:  # initial delay + one interval → stop the loop
            raise asyncio.CancelledError

    monkeypatch.setattr(daemon, "_auto_update_check", fake_check)
    monkeypatch.setattr(daemon.asyncio, "sleep", fake_sleep)
    orch = _StubOrchestrator()
    with contextlib.suppress(asyncio.CancelledError):
        await daemon._auto_update_loop(orch)
    assert sleeps[0] == daemon._AUTO_UPDATE_INITIAL_DELAY
    assert sleeps[1] == daemon._AUTO_UPDATE_CHECK_INTERVAL
    assert checks == [orch]


def test_extract_title_skips_leading_blank_lines():
    # Leading blank lines must be skipped so the first *content* line decides
    # whether the body opens with a heading.
    from claude_dingtalk_bridge.daemon import _extract_title

    assert _extract_title("\n\n### Heads up\nbody") == "Heads up"


def test_extract_title_returns_none_when_no_leading_heading():
    # Only a leading heading counts; a body that opens with prose has no title.
    from claude_dingtalk_bridge.daemon import _extract_title

    assert _extract_title("hello world\n### too late") is None
    assert _extract_title("") is None
    assert _extract_title("\n\n") is None
