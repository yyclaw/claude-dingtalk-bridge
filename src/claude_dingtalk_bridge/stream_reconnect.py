"""Backoff state machine for DingTalk Stream reconnect attempts.

The DingTalk gateway penalises rapid reconnects with prolonged
``RemoteDisconnected`` lockouts (~30 min observed). The SDK's built-in loop
retries every 10s with no awareness of this, so a brief network blip can
escalate into a half-hour of dropped messages (DingTalk doesn't queue inbound
messages for offline bots — confirmed empirically).

This module keeps the state out of the reconnect loop so it can be unit-tested
without touching the network.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass
class ReconnectState:
    """Tracks consecutive failures and returns the next backoff delay.

    A connection that stayed up for at least ``stable_threshold`` seconds is
    treated as a fresh start — the next disconnect is uncorrelated with prior
    outages and gets the shortest delay. A shorter run is treated as still
    inside the same outage and keeps the backoff climbing.
    """

    delays: tuple[float, ...] = (10.0, 30.0, 90.0, 300.0)
    stable_threshold: float = 60.0
    jitter: bool = True
    _rand: random.Random = field(default_factory=random.Random)

    _failure_count: int = 0

    def on_disconnect(self, connection_duration: float | None) -> float:
        """Record a disconnect; return the seconds to wait before retrying.

        ``connection_duration`` is how long the just-ended connection stayed
        live (``None`` if open_connection or the websocket handshake never
        succeeded). The caller is expected to ``await asyncio.sleep(delay)``
        before retrying.
        """
        if (
            connection_duration is not None
            and connection_duration >= self.stable_threshold
        ):
            self._failure_count = 0

        self._failure_count += 1
        idx = min(self._failure_count - 1, len(self.delays) - 1)
        base = self.delays[idx]
        if self.jitter:
            return base * (0.5 + self._rand.random())
        return base
