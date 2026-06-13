"""Wake- and network-recovery detection for the reconnect loop.

Two small observers feed a shared "retry-now" signal so the DingTalk Stream
reconnect loop can abandon a long backoff sleep the moment the machine wakes
or the network returns, instead of waiting the timer out.

Everything here is pure or dependency-injected so it unit-tests without
touching the clock, the network, or the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

WAKE_TICK = 5.0
WAKE_SKEW_THRESHOLD = 10.0
REACH_TICK = 15.0
# Only used to force the kernel to pick the default route. UDP connect() sends
# no packet, so this address is never actually contacted — its only job is to
# be a public, routable IP (not loopback/link-local/private). Any such IP works
# and the port is arbitrary; 223.5.5.5 (AliDNS) is a stable, China-routable
# anchor. This is NOT a ping of AliDNS.
ANCHOR_IP = "223.5.5.5"


class WakeWatcher:
    """Detects a sleep→wake transition from wall-vs-monotonic clock skew.

    On macOS ``time.monotonic()`` pauses during system sleep while
    ``time.time()`` keeps real time. After a wake the first tick sees wall
    advance far more than monotonic; a skew over ``threshold`` means the
    process was suspended and just resumed.
    """

    def __init__(self, *, threshold: float = WAKE_SKEW_THRESHOLD) -> None:
        self._threshold = threshold
        self._last_wall: float | None = None
        self._last_mono: float | None = None

    def tick(self, *, wall: float, mono: float) -> bool:
        prev_wall, prev_mono = self._last_wall, self._last_mono
        self._last_wall, self._last_mono = wall, mono
        if prev_wall is None or prev_mono is None:
            return False
        skew = (wall - prev_wall) - (mono - prev_mono)
        return skew > self._threshold


class ReachabilityWatcher:
    """Edge-triggers on a network ``False→True`` (unreachable→reachable).

    Starts in the "unreachable" state, so the first reachable reading is itself
    a recovery edge. Only the rising edge fires — a steady reachable state and
    a drop to unreachable both return False.
    """

    def __init__(self) -> None:
        self._reachable = False

    def update(self, reachable: bool) -> bool:
        edge = reachable and not self._reachable
        self._reachable = reachable
        return edge


def has_default_route(anchor_ip: str = ANCHOR_IP) -> bool:
    """Return whether the host currently has a usable default route.

    Sends nothing: a UDP ``connect()`` only triggers a kernel route lookup and
    records a default peer. Success means an interface is up with a default
    gateway; any ``OSError`` means no usable route → ``False``. Socket creation
    is guarded too (``ENETUNREACH`` on connect, but also e.g. ``EMFILE`` under
    fd exhaustion at creation): the probe must never raise, or it would escape
    ``watch_reachability`` and kill it for the rest of the daemon's life.
    ``anchor_ip`` is never contacted — see ``ANCHOR_IP``.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return False
    try:
        sock.connect((anchor_ip, 80))
        sock.getsockname()
        return True
    except OSError:
        return False
    finally:
        sock.close()


async def watch_wake(
    *,
    on_wake: Callable[[], None],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    wall_clock: Callable[[], float] = time.time,
    mono_clock: Callable[[], float] = time.monotonic,
    interval: float = WAKE_TICK,
    threshold: float = WAKE_SKEW_THRESHOLD,
) -> None:
    """Tick every ``interval`` seconds; call ``on_wake`` on a sleep→wake skew.

    Runs only while the process is awake — the ``sleep`` timer is frozen during
    system sleep, so the first tick after a wake is what observes the skew.
    """
    watcher = WakeWatcher(threshold=threshold)
    while True:
        await sleep(interval)
        try:
            if watcher.tick(wall=wall_clock(), mono=mono_clock()):
                on_wake()
        except Exception:  # noqa: BLE001 - one bad tick must not kill the watcher
            logger.warning("wake watcher tick failed", exc_info=True)


async def _default_route_probe() -> bool:
    return await asyncio.to_thread(has_default_route)


async def watch_reachability(
    *,
    is_disconnected: Callable[[], bool],
    on_recover: Callable[[], None],
    probe: Callable[[], Awaitable[bool]] = _default_route_probe,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    interval: float = REACH_TICK,
) -> None:
    """Tick every ``interval`` seconds; call ``on_recover`` on a network return.

    Probes only while ``is_disconnected()`` is true, so a healthy connection
    incurs no probing. The probe sends no traffic (see ``has_default_route``),
    and only a ``False→True`` edge fires ``on_recover``.
    """
    watcher = ReachabilityWatcher()
    while True:
        await sleep(interval)
        try:
            if not is_disconnected():
                continue
            if watcher.update(await probe()):
                on_recover()
        except Exception:  # noqa: BLE001 - one bad probe must not kill the watcher
            logger.warning("reachability watcher probe failed", exc_info=True)
