import asyncio
import contextlib

import claude_dingtalk_bridge.connectivity as conn
from claude_dingtalk_bridge.connectivity import (
    ReachabilityWatcher,
    WakeWatcher,
)


# --- WakeWatcher --------------------------------------------------------


def test_wake_watcher_first_tick_never_fires():
    # No prior reading to compare against.
    w = WakeWatcher(threshold=10.0)
    assert w.tick(wall=1000.0, mono=500.0) is None


def test_wake_watcher_returns_suspended_span_when_wall_outruns_mono():
    w = WakeWatcher(threshold=10.0)
    w.tick(wall=1000.0, mono=500.0)
    # 600s of wall elapsed but only 5s of monotonic → suspended ~595s.
    assert w.tick(wall=1600.0, mono=505.0) == 595.0


def test_wake_watcher_quiet_during_normal_ticks():
    w = WakeWatcher(threshold=10.0)
    w.tick(wall=1000.0, mono=500.0)
    # Awake: wall and mono advance together.
    assert w.tick(wall=1005.0, mono=505.0) is None


def test_wake_watcher_threshold_is_exclusive():
    w = WakeWatcher(threshold=10.0)
    w.tick(wall=1000.0, mono=500.0)
    # skew exactly 10.0 must NOT fire (> not >=) — avoids edge jitter firing.
    assert w.tick(wall=1015.0, mono=505.0) is None


# --- ReachabilityWatcher ------------------------------------------------


def test_reachability_watcher_fires_only_on_false_to_true():
    w = ReachabilityWatcher()
    assert w.update(False) is False  # local network dropped
    assert w.update(True) is True    # recovered → edge
    assert w.update(True) is False   # already up, no repeat
    assert w.update(False) is False  # went down, not an edge we fire on


def test_reachability_watcher_starts_reachable_so_first_true_is_not_an_edge():
    # Entering backoff usually means the gateway pushed back, not that the local
    # network dropped — so the first reachable probe must NOT read as a recovery.
    w = ReachabilityWatcher()
    assert w.update(True) is False


# --- has_default_route --------------------------------------------------


def test_has_default_route_true_when_socket_connects(monkeypatch):
    calls = {}

    class _FakeSock:
        def connect(self, addr):
            calls["addr"] = addr

        def getsockname(self):
            return ("192.168.1.50", 51234)

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(conn.socket, "socket", lambda *a, **k: _FakeSock())
    assert conn.has_default_route() is True
    # The anchor IP is what we route toward; port is arbitrary.
    assert calls["addr"][0] == conn.ANCHOR_IP
    assert calls["closed"] is True


def test_has_default_route_false_when_connect_raises(monkeypatch):
    closed = {}

    class _FakeSock:
        def connect(self, addr):
            raise OSError("Network is unreachable")

        def getsockname(self):  # pragma: no cover - never reached
            return ("0.0.0.0", 0)

        def close(self):
            closed["closed"] = True

    monkeypatch.setattr(conn.socket, "socket", lambda *a, **k: _FakeSock())
    assert conn.has_default_route() is False
    # Socket is closed even on the failure path.
    assert closed["closed"] is True


def test_has_default_route_false_when_socket_creation_raises(monkeypatch):
    # socket.socket() itself raising (e.g. EMFILE under fd exhaustion) must be
    # swallowed and reported as "no route", not escape and kill the watcher.
    def _boom(*a, **k):
        raise OSError("Too many open files")

    monkeypatch.setattr(conn.socket, "socket", _boom)
    assert conn.has_default_route() is False


# --- wake classification ------------------------------------------------


# `pmset -g systemstate` reports the *live* current power state. A full/awake
# state lists the Graphics capability; a DarkWake (CPU/network up for
# maintenance, display subsystem down) omits it.
_SYSTEMSTATE_FULL = (
    "Current System Capabilities are: CPU Graphics Audio Network \n"
    "Current Power State: 4\n"
)
_SYSTEMSTATE_DARK = (
    "Current System Capabilities are: CPU Network \n"
    "Current Power State: 1\n"
)


def test_parse_capabilities_full_wake_has_graphics():
    assert conn.parse_capabilities_are_dark(_SYSTEMSTATE_FULL) is False


def test_parse_capabilities_dark_wake_lacks_graphics():
    # The 01:00 spurious-reconnect case: a 2s maintenance DarkWake. Read from the
    # live state, Graphics is absent the instant the wake happens — no race
    # against pmset's asynchronous log write, no recency window to age out.
    assert conn.parse_capabilities_are_dark(_SYSTEMSTATE_DARK) is True


def test_parse_capabilities_none_when_no_capability_line():
    # Without the capabilities line we can't tell; callers fail open.
    assert conn.parse_capabilities_are_dark("Current Power State: 4\n") is None


def test_parse_capabilities_matches_whole_token_not_substring():
    # Capabilities are whitespace-separated tokens; a token that merely contains
    # the letters must not be read as the Graphics capability.
    log = "Current System Capabilities are: CPU GraphicsX Network \n"
    assert conn.parse_capabilities_are_dark(log) is True


def test_pmset_systemstate_invokes_pmset(monkeypatch):
    captured = {}

    class _Proc:
        stdout = "the state"

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(conn.subprocess, "run", fake_run)
    assert conn._pmset_systemstate() == "the state"
    assert captured["cmd"] == conn.PMSET_STATE_CMD
    assert captured["kwargs"]["timeout"] == conn.PMSET_TIMEOUT


async def test_wake_is_dark_true_for_dark_wake(monkeypatch):
    monkeypatch.setattr(conn, "_pmset_systemstate", lambda: _SYSTEMSTATE_DARK)
    assert await conn.wake_is_dark() is True


async def test_wake_is_dark_false_for_full_wake(monkeypatch):
    monkeypatch.setattr(conn, "_pmset_systemstate", lambda: _SYSTEMSTATE_FULL)
    assert await conn.wake_is_dark() is False


async def test_wake_is_dark_fails_open_when_no_capability_line(monkeypatch):
    # A truncated/empty readout we can't classify fails open to non-dark, so a
    # real wake still reconnects rather than getting stuck offline.
    monkeypatch.setattr(conn, "_pmset_systemstate", lambda: "")
    assert await conn.wake_is_dark() is False


async def test_wake_is_dark_fails_open_when_pmset_raises(monkeypatch, caplog):
    def boom():
        raise OSError("pmset missing")

    monkeypatch.setattr(conn, "_pmset_systemstate", boom)
    assert await conn.wake_is_dark() is False
    assert "pmset systemstate probe failed" in caplog.text


# --- async drivers ------------------------------------------------------


def _draining_sleep(n_before_stop):
    """Return an async sleep that returns n times, then raises CancelledError."""
    state = {"n": 0}

    async def _sleep(_delay):
        if state["n"] >= n_before_stop:
            raise asyncio.CancelledError
        state["n"] += 1

    return _sleep


async def test_watch_wake_nudges_only_while_disconnected():
    # A detected wake nudges on_wake (no span — the loop measures the outage and
    # classifies the wake); the watcher is a pure backoff-interrupter.
    fired = []
    walls = iter([100.0, 100.0, 700.0])  # 3rd reading jumps 600s
    monos = iter([10.0, 10.0, 15.0])     # monotonic barely moved
    sleep = _draining_sleep(3)

    with contextlib.suppress(asyncio.CancelledError):
        await conn.watch_wake(
            is_disconnected=lambda: True,
            on_wake=lambda: fired.append(True),
            sleep=sleep,
            wall_clock=lambda: next(walls),
            mono_clock=lambda: next(monos),
            interval=5.0,
            threshold=10.0,
        )
    assert fired == [True]


async def test_watch_wake_quiet_while_connected_despite_skew():
    # Connected: a wake is irrelevant — the socket I/O surfaces any death itself.
    # The watcher still ticks (to keep its baseline fresh) but never nudges.
    fired = []
    walls = iter([100.0, 100.0, 700.0])
    monos = iter([10.0, 10.0, 15.0])
    sleep = _draining_sleep(3)

    with contextlib.suppress(asyncio.CancelledError):
        await conn.watch_wake(
            is_disconnected=lambda: False,
            on_wake=lambda: fired.append(True),
            sleep=sleep,
            wall_clock=lambda: next(walls),
            mono_clock=lambda: next(monos),
            interval=5.0,
            threshold=10.0,
        )
    assert fired == []


async def test_watch_wake_quiet_when_no_skew():
    fired = []
    walls = iter([100.0, 105.0, 110.0])
    monos = iter([10.0, 15.0, 20.0])
    sleep = _draining_sleep(3)

    with contextlib.suppress(asyncio.CancelledError):
        await conn.watch_wake(
            is_disconnected=lambda: True,
            on_wake=lambda: fired.append(True),
            sleep=sleep,
            wall_clock=lambda: next(walls),
            mono_clock=lambda: next(monos),
            interval=5.0,
            threshold=10.0,
        )
    assert fired == []


async def test_watch_wake_survives_tick_error(caplog):
    # A raising clock/tick must be caught and logged, not end the watcher.
    fired = []
    calls = {"n": 0}

    def boom_then_quiet():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("clock boom")
        return 100.0

    sleep = _draining_sleep(1)
    with contextlib.suppress(asyncio.CancelledError):
        await conn.watch_wake(
            is_disconnected=lambda: True,
            on_wake=lambda: fired.append(True),
            sleep=sleep,
            wall_clock=boom_then_quiet,
            mono_clock=lambda: 10.0,
            interval=5.0,
            threshold=10.0,
        )
    assert fired == []  # error swallowed; on_wake never reached
    assert "wake watcher tick failed" in caplog.text


async def test_watch_reachability_probes_only_while_disconnected():
    recovered = []
    # Disconnected for the first two ticks, then connected.
    disc = iter([True, True, False])
    # Probe: down, then up → a recovery edge on the 2nd disconnected tick.
    probe_results = iter([False, True])
    sleep = _draining_sleep(3)

    async def fake_probe():
        return next(probe_results)

    with contextlib.suppress(asyncio.CancelledError):
        await conn.watch_reachability(
            is_disconnected=lambda: next(disc),
            probe=fake_probe,
            on_recover=lambda: recovered.append(True),
            sleep=sleep,
            interval=15.0,
        )
    # Probe ran twice (only while disconnected), recovery fired once.
    assert recovered == [True]


async def test_watch_reachability_skips_probe_when_connected():
    recovered = []
    disc = iter([False, False, False])
    sleep = _draining_sleep(3)

    async def fake_probe():  # pragma: no cover - must never be called
        raise AssertionError("probe ran while connected")

    with contextlib.suppress(asyncio.CancelledError):
        await conn.watch_reachability(
            is_disconnected=lambda: next(disc),
            probe=fake_probe,
            on_recover=lambda: recovered.append(True),
            sleep=sleep,
            interval=15.0,
        )
    assert recovered == []


async def test_watch_reachability_survives_probe_error(caplog):
    # A raising probe must be caught and logged; the watcher keeps running and
    # still fires on the next genuine recovery edge (a False then a True).
    recovered = []
    disc = iter([True, True, True])
    results = iter([RuntimeError("boom"), False, True])

    async def flaky_probe():
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    sleep = _draining_sleep(3)
    with contextlib.suppress(asyncio.CancelledError):
        await conn.watch_reachability(
            is_disconnected=lambda: next(disc),
            probe=flaky_probe,
            on_recover=lambda: recovered.append(True),
            sleep=sleep,
            interval=15.0,
        )
    # 1st tick: probe raised → swallowed. 2nd: False recorded. 3rd: True → edge.
    assert recovered == [True]
    assert "reachability watcher probe failed" in caplog.text


async def test_default_route_probe_delegates_to_has_default_route(monkeypatch):
    monkeypatch.setattr(conn, "has_default_route", lambda: True)
    assert await conn._default_route_probe() is True
