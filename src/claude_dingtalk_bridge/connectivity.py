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
import subprocess
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

WAKE_TICK = 5.0
WAKE_SKEW_THRESHOLD = 10.0
REACH_TICK = 15.0
# `pmset -g systemstate` is read only when the reconnect loop is deciding whether
# to skip its backoff (rare), in a thread; cap it so a hung subprocess can't
# wedge it.
PMSET_STATE_CMD = ("/usr/bin/pmset", "-g", "systemstate")
PMSET_TIMEOUT = 10.0
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

    def tick(self, *, wall: float, mono: float) -> float | None:
        """Return the suspended span (seconds) if this tick follows a wake.

        ``None`` when no wake is detected. The value is the wall time the process
        was suspended *since the previous tick* — for an overnight sleep punctured
        by DarkWakes that is the gap to the last DarkWake, not the whole night, so
        callers that want the total outage must sum it across wakes.
        """
        prev_wall, prev_mono = self._last_wall, self._last_mono
        self._last_wall, self._last_mono = wall, mono
        if prev_wall is None or prev_mono is None:
            return None
        skew = (wall - prev_wall) - (mono - prev_mono)
        return skew if skew > self._threshold else None


def parse_capabilities_are_dark(systemstate_output: str) -> bool | None:
    """Classify the system's *current* power state from ``pmset -g systemstate``.

    The output carries a line ``Current System Capabilities are: CPU Graphics
    Audio Network``. A macOS **DarkWake** brings the CPU and network up for
    maintenance (Power Nap, keepalive) but leaves the graphics subsystem down, so
    the ``Graphics`` capability is the authoritative discriminator
    (``kIOPMSystemCapabilityGraphics``): present → a full/awake state, absent → a
    dark wake. Display *idle*-sleep keeps ``Graphics`` (the system is still fully
    awake, only the panel is off), so an awake screen-off network outage is not
    misread as dark — verified on macOS 26.

    Returns ``True`` for a dark wake, ``False`` for a full/awake state, ``None``
    if the capabilities line isn't present (so callers can fail open).

    Unlike the previous ``pmset -g log`` scrape this reads the *live current*
    state: there is no race against pmset's asynchronous log write (which once
    let a 2s maintenance DarkWake reconnect because its log row wasn't published
    yet) and no recency window to age a long maintenance wake out — the capability
    flips synchronously with the wake transition itself.
    """
    marker = "Current System Capabilities are:"
    for line in systemstate_output.splitlines():
        _, sep, caps = line.partition(marker)
        if sep:
            return "Graphics" not in caps.split()
    return None


def _pmset_systemstate() -> str:
    return subprocess.run(
        PMSET_STATE_CMD,
        capture_output=True,
        text=True,
        timeout=PMSET_TIMEOUT,
        check=False,
    ).stdout


async def wake_is_dark() -> bool:
    """Whether the system is *currently* in a macOS DarkWake, not a full wake.

    Reads the live power state via ``pmset -g systemstate`` and classifies on the
    Graphics capability — see ``parse_capabilities_are_dark``.

    Fail-open: any uncertainty (pmset failure, no capabilities line) returns
    ``False`` so the reconnect still fires — a spurious reconnect is cheap, a
    missed one leaves the phone unable to reach the daemon after the lid opens.
    """
    try:
        text = await asyncio.to_thread(_pmset_systemstate)
    except Exception:  # noqa: BLE001 - a probe failure must not block reconnect
        logger.warning("pmset systemstate probe failed", exc_info=True)
        return False
    return parse_capabilities_are_dark(text) is True


class ReachabilityWatcher:
    """Edge-triggers on a network ``False→True`` (unreachable→reachable).

    Starts in the "reachable" state: the daemon enters backoff far more often
    because the gateway pushed back than because the local network dropped, so
    assuming reachable avoids firing a bogus recovery edge on the first probe of
    an outage that never lost the network. A genuine local drop is still caught —
    the probe records the ``False`` first, then fires on the return. Only the
    rising edge fires; a steady reachable state and a drop both return False.
    """

    def __init__(self) -> None:
        self._reachable = True

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
    is_disconnected: Callable[[], bool],
    on_wake: Callable[[], None],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    wall_clock: Callable[[], float] = time.time,
    mono_clock: Callable[[], float] = time.monotonic,
    interval: float = WAKE_TICK,
    threshold: float = WAKE_SKEW_THRESHOLD,
) -> None:
    """Tick every ``interval`` seconds; nudge ``on_wake`` on a sleep→wake.

    Runs only while the process is awake — the ``sleep`` timer is frozen during
    system sleep, so the first tick after a wake is what observes the skew. The
    skew is measured on *every* tick (to keep the baseline fresh), but ``on_wake``
    is nudged only while ``is_disconnected()`` — a wake matters solely to cut a
    backoff short. While connected a wake is irrelevant: the socket's own I/O
    surfaces any death, and a DarkWake (radios briefly up for maintenance) would
    otherwise flap the daemon. ``on_wake`` carries no payload; the reconnect loop
    measures the outage and classifies the wake (full vs DarkWake) itself, so the
    ``pmset`` probe stays off the hot path while connected.
    """
    watcher = WakeWatcher(threshold=threshold)
    while True:
        await sleep(interval)
        try:
            offline = watcher.tick(wall=wall_clock(), mono=mono_clock())
            if offline is None or not is_disconnected():
                continue
            logger.info("woke from sleep during backoff; re-checking connection")
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

    A DarkWake briefly brings the network up (the radios wake to service Power
    Nap), which reads as a recovery edge and would flap the daemon if it
    reconnected. That suppression is no longer this watcher's job: ``on_recover``
    only nudges the reconnect loop, which classifies the wake (via ``pmset``)
    before acting and stays in backoff for a DarkWake — so the gate lives in one
    place and this watcher never forks a subprocess.
    """
    watcher = ReachabilityWatcher()
    while True:
        await sleep(interval)
        try:
            if not is_disconnected():
                continue
            if watcher.update(await probe()):
                logger.info("network reachable again; re-evaluating connection")
                on_recover()
        except Exception:  # noqa: BLE001 - one bad probe must not kill the watcher
            logger.warning("reachability watcher probe failed", exc_info=True)
