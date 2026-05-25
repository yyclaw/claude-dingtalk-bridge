from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from claude_dingtalk_bridge.config import GeoConfig

logger = logging.getLogger(__name__)

# A successful check is reused for this long so a burst of back-to-back
# turns shares one query. Kept short on purpose: a stale ok result is a
# window where a VPN drop goes unnoticed.
GEO_CACHE_TTL_SECONDS = 30.0


@dataclass
class GeoCheck:
    ok: bool
    detail: str  # phone-facing message, worded to match cc.fish


def check_geo(cfg: GeoConfig) -> GeoCheck:
    """Query the exit IP's country through the local proxy.

    Any network/parse failure is caught and returned as a non-ok result so
    the orchestrator never has to handle an exception here. Message wording
    mirrors the ``cc.fish`` shell function this gate is modeled on.
    """
    proxies = {"http": cfg.proxy_url, "https": cfg.proxy_url}
    try:
        resp = requests.get(
            cfg.geo_service, proxies=proxies, timeout=cfg.timeout_seconds
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - any failure becomes a non-ok result
        logger.warning("Geo check failed: %s", exc)
        return GeoCheck(ok=False, detail="❌ Connect to the VPN first.")

    if data.get("status") != "success":
        return GeoCheck(ok=False, detail="❌ Failed to parse geolocation data.")

    code = data.get("countryCode")
    ip = data.get("query", "")
    if not code:
        return GeoCheck(ok=False, detail="❌ Country code not found in response.")

    if code != cfg.target_country:
        return GeoCheck(
            ok=False,
            detail=f"📍 IP: {ip}\n❌ IP location: {code} (expected: {cfg.target_country})",
        )
    return GeoCheck(
        ok=True,
        detail=f"📍 IP: {ip}\n✅ IP location verified: {cfg.target_country}",
    )


class CachedGeoCheck:
    """Wraps :func:`check_geo` with a short TTL.

    Only successful results are cached: a failure always re-checks on the
    next turn, so a user who just fixed the VPN isn't told "no" for the
    full TTL after resending.
    """

    def __init__(
        self, cfg: GeoConfig, ttl_seconds: float = GEO_CACHE_TTL_SECONDS
    ) -> None:
        self._cfg = cfg
        self._ttl = ttl_seconds
        self._cached: GeoCheck | None = None
        self._fetched_at = 0.0

    def __call__(self) -> GeoCheck:
        now = time.monotonic()
        if self._cached is not None and now - self._fetched_at < self._ttl:
            logger.debug("Geo cache hit (age %.1fs)", now - self._fetched_at)
            return self._cached
        logger.debug("Geo cache miss, querying exit IP")
        result = check_geo(self._cfg)
        self._cached = result if result.ok else None
        self._fetched_at = now
        return result
