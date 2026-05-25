import asyncio
import contextlib

import websockets

from claude_dingtalk_bridge.config import Config, GeoConfig, PermissionRules, Project
from claude_dingtalk_bridge.daemon import (
    _ChatHandler,
    _disable_websocket_proxy,
    _silence_cancellederror_noise,
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
        permissions=PermissionRules(
            allowed_tools=["Read"], allowed_bash=[], allow_edits_in_project=True
        ),
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
    await orchestrator._send_markdown("# hi")
    assert calls == [("md", "staff-1", "Claude", "# hi")]


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
    # Skip the reconnect sleep so KeyboardInterrupt tests finish quickly.
    monkeypatch.setattr(daemon, "_RECONNECT_DELAY", 0.0)
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


def test_silence_cancellederror_noise_drops_cancellederror_records():
    import logging as _logging

    _silence_cancellederror_noise()
    sdk_logger = _logging.getLogger("dingtalk_stream.client")
    captured: list[str] = []

    class _Capture(_logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = _Capture(level=_logging.DEBUG)
    sdk_logger.addHandler(handler)
    sdk_logger.setLevel(_logging.DEBUG)
    try:
        # Shutdown path: the SDK passes the CancelledError object as the arg --
        # filter drops it regardless of the format string the SDK happens to use.
        sdk_logger.error("[start] network exception, error=%s", asyncio.CancelledError())
        sdk_logger.error("anything at all, error=%s", asyncio.CancelledError())
        # Real network error: arg is a different exception -> passes through.
        sdk_logger.error(
            "[start] network exception, error=%s",
            ConnectionResetError("peer reset"),
        )
        # No-arg log line: passes through.
        sdk_logger.error("open connection failed")
    finally:
        sdk_logger.removeHandler(handler)
    assert captured == [
        "[start] network exception, error=peer reset",
        "open connection failed",
    ]


async def test_drive_stream_client_reconnects_on_exception(monkeypatch):
    monkeypatch.setattr(daemon, "_RECONNECT_DELAY", 0.0)

    class _FlakyClient:
        def __init__(self):
            self.calls = 0

        async def start(self):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("network blip")
            raise asyncio.CancelledError  # break the loop on the 3rd attempt

    client = _FlakyClient()
    with contextlib.suppress(asyncio.CancelledError):
        await daemon._drive_stream_client(client)
    assert client.calls == 3


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
    # Sender id is masked to first-3 + ****** + last-3; raw value must not leak.
    masked = "sta******f-1"
    assert all("staff-1" not in m for m in inbound)
    assert any("msgtype=text" in m and masked in m for m in inbound)
    assert any("msgtype=audio" in m and masked in m for m in inbound)
    assert any("msgtype=file" in m and masked in m for m in inbound)


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
    # client.start().
    monkeypatch.setattr(daemon, "_RECONNECT_DELAY", 5.0)

    class _AlwaysFailingClient:
        async def start(self):
            raise RuntimeError("blip")

    task = asyncio.create_task(daemon._drive_stream_client(_AlwaysFailingClient()))
    # Yield enough times for start() to fail and the sleep to begin.
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
