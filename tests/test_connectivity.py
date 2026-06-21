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


# Real pmset rows: domain column is space-padded and tab-separated from the body.
_PMSET_SAMPLE = (
    "2026-06-20 10:53:21 +0800 DarkWake            \tDarkWake from Deep Idle : rtc 2 secs\n"
    "short line\n"
    "2026-06-20 11:00:00 +0800 Sleep               \tEntering Sleep state\n"
    "2026-06-20 11:18:03 +0800 Wake                \tWake from Deep Idle : lid HID 3 secs\n"
)


def test_parse_wake_is_dark_picks_latest_full_wake():
    # Scanning from the tail, the newest (completed) wake row is a full Wake.
    assert conn.parse_wake_is_dark(_PMSET_SAMPLE) is False


def test_parse_wake_is_dark_skips_malformed_tail_rows():
    # A short/malformed row newer than the last real wake is skipped by the
    # tail-first scan rather than mistaken for an event.
    log = (
        "2026-06-20 11:18:03 +0800 Wake                \tWake from Deep Idle\n"
        "short line\n"
    )
    assert conn.parse_wake_is_dark(log) is False


def test_parse_wake_is_dark_picks_latest_dark_wake():
    log = (
        "2026-06-20 11:18:03 +0800 Wake                \tWake from Deep Idle : lid\n"
        "2026-06-20 11:33:00 +0800 DarkWake            \tDarkWake from Deep Idle\n"
    )
    assert conn.parse_wake_is_dark(log) is True


def test_parse_wake_is_dark_ignores_wake_requests_schedule_rows():
    # "Wake Requests" (and WakeTime/WakeDetails) start with the token "Wake" but
    # are NOT wake events — a naive split misreads them and flips a DarkWake to a
    # full wake. The latest real event here is a DarkWake, so it stays dark.
    log = (
        "2026-06-20 15:49:09 +0800 DarkWake            \tDarkWake from Deep Idle\n"
        "2026-06-20 15:49:56 +0800 Wake Requests       \t[*process=dasd request=x]\n"
        "2026-06-20 15:50:00 +0800 WakeTime            \tWakeTime stats\n"
    )
    assert conn.parse_wake_is_dark(log) is True


def test_parse_wake_is_dark_none_when_no_wake_line():
    row = "2026-06-20 11:00:00 +0800 Sleep               \tEntering Sleep state\n"
    assert conn.parse_wake_is_dark(row) is None


def _epoch(stamp: str) -> float:
    from datetime import datetime

    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S %z").timestamp()


def test_parse_wake_is_dark_in_progress_wake_ignores_recency_window():
    # A DarkWake still in progress (no completed "N secs" suffix yet) is the
    # current power state, so it classifies dark even though it *started* well
    # outside the recency window — the 10:53 lid-shut "wake/network return"
    # misread, where a 125s maintenance DarkWake had aged out by its start time.
    log = "2026-06-20 15:58:00 +0800 DarkWake            \tDarkWake from Deep Idle\n"
    now = _epoch("2026-06-20 15:59:00 +0800")  # 60s after start, still running
    assert conn.parse_wake_is_dark(log, now=now, max_age=30.0) is True


def test_parse_wake_is_dark_excludes_stale_wake_outside_window():
    # A *completed* DarkWake that ended hours ago must not be classified as "the
    # wake that just happened" — otherwise a genuine network recovery would be
    # wrongly suppressed. Recency is judged by its end (start + duration).
    log = (
        "2026-06-20 10:00:00 +0800 DarkWake            "
        "\tDarkWake from Deep Idle 120 secs\n"
    )
    now = _epoch("2026-06-20 16:00:00 +0800")  # 6h after it ended
    assert conn.parse_wake_is_dark(log, now=now, max_age=30.0) is None


def test_parse_wake_is_dark_keeps_recent_wake_within_window():
    # A completed DarkWake that ended just now is within the window.
    log = (
        "2026-06-20 15:59:50 +0800 DarkWake            "
        "\tDarkWake from Deep Idle 5 secs\n"
    )
    now = _epoch("2026-06-20 16:00:00 +0800")  # ended 15:59:55, 5s ago
    assert conn.parse_wake_is_dark(log, now=now, max_age=30.0) is True


def test_parse_wake_is_dark_keeps_unparseable_timestamp_failing_open():
    # A completed row whose timestamp we can't parse is kept, not dropped: a real
    # wake we couldn't date must still classify rather than vanish.
    log = "2026-13-99 25:61:61 +0800 Wake                \tWake from Deep Idle 5 secs\n"
    now = _epoch("2026-06-20 16:00:00 +0800")
    assert conn.parse_wake_is_dark(log, now=now, max_age=30.0) is False


async def test_wake_is_dark_ignores_stale_wake(monkeypatch):
    # The reconnect loop passes `now`; a DarkWake older than the window reads as
    # "no recent wake" → False (fail open), so a real recovery still reconnects.
    monkeypatch.setattr(
        conn, "_pmset_log",
        lambda: "2026-06-20 10:00:00 +0800 DarkWake            \tDarkWake 120 secs\n",
    )
    now = _epoch("2026-06-20 16:00:00 +0800")
    assert await conn.wake_is_dark(now=now, max_age=30.0) is False


def test_pmset_log_invokes_pmset(monkeypatch):
    captured = {}

    class _Proc:
        stdout = "the log"

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(conn.subprocess, "run", fake_run)
    assert conn._pmset_log() == "the log"
    assert captured["cmd"] == conn.PMSET_CMD
    assert captured["kwargs"]["timeout"] == conn.PMSET_TIMEOUT


async def test_wake_is_dark_true_for_dark_wake(monkeypatch):
    monkeypatch.setattr(
        conn, "_pmset_log",
        lambda: "2026 11:33:00 +0800 DarkWake            \tDarkWake from Deep Idle",
    )
    assert await conn.wake_is_dark() is True


async def test_wake_is_dark_false_for_full_wake(monkeypatch):
    monkeypatch.setattr(
        conn, "_pmset_log",
        lambda: "2026 11:18:03 +0800 Wake                \tWake from Deep Idle lid",
    )
    assert await conn.wake_is_dark() is False


async def test_wake_is_dark_fails_open_when_pmset_raises(monkeypatch, caplog):
    def boom():
        raise OSError("pmset missing")

    monkeypatch.setattr(conn, "_pmset_log", boom)
    assert await conn.wake_is_dark() is False
    assert "pmset wake-type probe failed" in caplog.text


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
