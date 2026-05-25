import requests

from claude_dingtalk_bridge.config import GeoConfig
from claude_dingtalk_bridge.geo import CachedGeoCheck, GeoCheck, check_geo


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _cfg() -> GeoConfig:
    return GeoConfig(
        proxy_url="http://127.0.0.1:8118",
        target_country="US",
        geo_service="http://geo.test/json",
        timeout_seconds=3,
    )


def test_matching_country_is_ok(monkeypatch):
    payload = {"status": "success", "countryCode": "US",
               "query": "1.2.3.4", "city": "LA", "country": "United States"}
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))
    result = check_geo(_cfg())
    assert isinstance(result, GeoCheck)
    assert result.ok is True


def test_wrong_country_not_ok(monkeypatch):
    payload = {"status": "success", "countryCode": "HK",
               "query": "45.8.1.1", "city": "Hong Kong", "country": "Hong Kong"}
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))
    result = check_geo(_cfg())
    assert result.ok is False
    assert "IP location: HK (expected: US)" in result.detail
    assert "45.8.1.1" in result.detail


def test_missing_country_code_not_ok(monkeypatch):
    payload = {"status": "success", "query": "1.2.3.4"}
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))
    result = check_geo(_cfg())
    assert result.ok is False
    assert result.detail == "❌ Country code not found in response."


def test_non_success_status_not_ok(monkeypatch):
    payload = {"status": "fail", "message": "reserved range"}
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))
    result = check_geo(_cfg())
    assert result.ok is False


def test_proxy_error_not_ok(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("proxy down")

    monkeypatch.setattr(requests, "get", boom)
    result = check_geo(_cfg())
    assert result.ok is False
    assert result.detail == "❌ Connect to the VPN first."


def test_matching_country_detail_shows_ip_and_country(monkeypatch):
    payload = {"status": "success", "countryCode": "US", "query": "1.2.3.4"}
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))
    result = check_geo(_cfg())
    assert result.detail == "📍 IP: 1.2.3.4\n✅ IP location verified: US"


def test_passes_proxy_to_requests(monkeypatch):
    seen = {}

    def capture(url, **kwargs):
        seen["url"] = url
        seen["proxies"] = kwargs.get("proxies")
        seen["timeout"] = kwargs.get("timeout")
        return FakeResponse({"status": "success", "countryCode": "US",
                             "query": "1.1.1.1", "city": "", "country": ""})

    monkeypatch.setattr(requests, "get", capture)
    check_geo(_cfg())
    assert seen["url"] == "http://geo.test/json"
    assert seen["proxies"] == {"http": "http://127.0.0.1:8118",
                               "https": "http://127.0.0.1:8118"}
    assert seen["timeout"] == 3


def test_cached_check_reuses_success_within_ttl(monkeypatch):
    calls = {"n": 0}

    def counted(*a, **k):
        calls["n"] += 1
        return FakeResponse({"status": "success", "countryCode": "US",
                             "query": "1.2.3.4"})

    monkeypatch.setattr(requests, "get", counted)
    cached = CachedGeoCheck(_cfg(), ttl_seconds=60)
    assert cached().ok is True
    assert cached().ok is True
    assert calls["n"] == 1  # second call served from cache


def test_cached_check_expires_after_ttl(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(
        {"status": "success", "countryCode": "US", "query": "1.2.3.4"}))
    clock = {"t": 1000.0}
    monkeypatch.setattr("claude_dingtalk_bridge.geo.time.monotonic",
                        lambda: clock["t"])
    cached = CachedGeoCheck(_cfg(), ttl_seconds=60)
    seen = []
    monkeypatch.setattr("claude_dingtalk_bridge.geo.check_geo",
                        lambda cfg: (seen.append(1), GeoCheck(True, "ok"))[1])
    cached()
    clock["t"] += 61
    cached()
    assert len(seen) == 2  # cache expired, re-queried


def test_cached_check_does_not_cache_failure(monkeypatch):
    calls = {"n": 0}

    def counted(*a, **k):
        calls["n"] += 1
        return FakeResponse({"status": "success", "countryCode": "HK",
                             "query": "45.8.1.1"})

    monkeypatch.setattr(requests, "get", counted)
    cached = CachedGeoCheck(_cfg(), ttl_seconds=60)
    assert cached().ok is False
    assert cached().ok is False
    assert calls["n"] == 2  # failures always re-checked
