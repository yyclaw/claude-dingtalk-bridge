import random

from claude_dingtalk_bridge.stream_reconnect import ReconnectState


def _state(**overrides):
    defaults = dict(
        delays=(10.0, 30.0, 90.0, 300.0),
        stable_threshold=60.0,
        jitter=False,
    )
    defaults.update(overrides)
    return ReconnectState(**defaults)


def test_first_failure_uses_first_delay():
    state = _state()
    assert state.on_disconnect(connection_duration=None) == 10.0


def test_consecutive_failures_climb_then_cap():
    state = _state()
    delays = [state.on_disconnect(None) for _ in range(6)]
    assert delays == [10.0, 30.0, 90.0, 300.0, 300.0, 300.0]


def test_stable_session_resets_backoff():
    state = _state()
    state.on_disconnect(None)
    state.on_disconnect(None)
    state.on_disconnect(None)
    # A connection that stayed up long enough wipes the slate; this event
    # itself counts as the first failure of the fresh outage (count == 1).
    assert state.on_disconnect(connection_duration=120.0) == 10.0


def test_short_session_does_not_reset_backoff():
    state = _state()
    state.on_disconnect(None)
    state.on_disconnect(None)
    # 5s of uptime isn't enough to count as recovery.
    assert state.on_disconnect(connection_duration=5.0) == 90.0


def test_jitter_bounds_delay_to_half_to_one_and_a_half_base():
    rng = random.Random(0)
    state = _state(jitter=True)
    state._rand = rng
    delays = [state.on_disconnect(None) for _ in range(20)]
    bases = (10.0, 30.0, 90.0, 300.0)
    for i, delay in enumerate(delays):
        base = bases[min(i, len(bases) - 1)]
        assert 0.5 * base <= delay <= 1.5 * base
