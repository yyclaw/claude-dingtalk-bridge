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
    assert w.tick(wall=1000.0, mono=500.0) is False


def test_wake_watcher_fires_when_wall_outruns_mono():
    w = WakeWatcher(threshold=10.0)
    w.tick(wall=1000.0, mono=500.0)
    # 600s of wall elapsed but only 5s of monotonic → slept ~595s, just woke.
    assert w.tick(wall=1600.0, mono=505.0) is True


def test_wake_watcher_quiet_during_normal_ticks():
    w = WakeWatcher(threshold=10.0)
    w.tick(wall=1000.0, mono=500.0)
    # Awake: wall and mono advance together.
    assert w.tick(wall=1005.0, mono=505.0) is False


def test_wake_watcher_threshold_is_exclusive():
    w = WakeWatcher(threshold=10.0)
    w.tick(wall=1000.0, mono=500.0)
    # skew exactly 10.0 must NOT fire (> not >=) — avoids edge jitter firing.
    assert w.tick(wall=1015.0, mono=505.0) is False


# --- ReachabilityWatcher ------------------------------------------------


def test_reachability_watcher_fires_only_on_false_to_true():
    w = ReachabilityWatcher()
    assert w.update(False) is False  # still down
    assert w.update(True) is True    # recovered → edge
    assert w.update(True) is False   # already up, no repeat
    assert w.update(False) is False  # went down, not an edge we fire on


def test_reachability_watcher_first_update_true_is_an_edge():
    # Initial state is "unknown/down"; the first True is a recovery edge.
    w = ReachabilityWatcher()
    assert w.update(True) is True


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


# --- async drivers ------------------------------------------------------


def _draining_sleep(n_before_stop):
    """Return an async sleep that returns n times, then raises CancelledError."""
    state = {"n": 0}

    async def _sleep(_delay):
        if state["n"] >= n_before_stop:
            raise asyncio.CancelledError
        state["n"] += 1

    return _sleep


async def test_watch_wake_calls_on_wake_when_skew_detected():
    fired = []
    walls = iter([100.0, 100.0, 700.0])  # 3rd reading jumps 600s
    monos = iter([10.0, 10.0, 15.0])     # monotonic barely moved
    sleep = _draining_sleep(3)

    with contextlib.suppress(asyncio.CancelledError):
        await conn.watch_wake(
            on_wake=lambda: fired.append(True),
            sleep=sleep,
            wall_clock=lambda: next(walls),
            mono_clock=lambda: next(monos),
            interval=5.0,
            threshold=10.0,
        )
    assert fired == [True]


async def test_watch_wake_quiet_when_no_skew():
    fired = []
    walls = iter([100.0, 105.0, 110.0])
    monos = iter([10.0, 15.0, 20.0])
    sleep = _draining_sleep(3)

    with contextlib.suppress(asyncio.CancelledError):
        await conn.watch_wake(
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
    # still fires on the next genuine recovery edge.
    recovered = []
    disc = iter([True, True])
    results = iter([RuntimeError("boom"), True])

    async def flaky_probe():
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    sleep = _draining_sleep(2)
    with contextlib.suppress(asyncio.CancelledError):
        await conn.watch_reachability(
            is_disconnected=lambda: next(disc),
            probe=flaky_probe,
            on_recover=lambda: recovered.append(True),
            sleep=sleep,
            interval=15.0,
        )
    # 1st tick: probe raised → swallowed. 2nd tick: probe True → recovery edge.
    assert recovered == [True]
    assert "reachability watcher probe failed" in caplog.text


async def test_default_route_probe_delegates_to_has_default_route(monkeypatch):
    monkeypatch.setattr(conn, "has_default_route", lambda: True)
    assert await conn._default_route_probe() is True
