import json
from unittest.mock import MagicMock, patch

import requests

from claude_dingtalk_bridge.dingtalk import DingTalkTransport


def _token_response():
    resp = MagicMock()
    resp.json.return_value = {"accessToken": "tok-1", "expireIn": 7200}
    resp.raise_for_status.return_value = None
    return resp


def _ok_response():
    resp = MagicMock()
    resp.json.return_value = {}
    resp.raise_for_status.return_value = None
    return resp


def test_send_text_posts_expected_payload():
    transport = DingTalkTransport("appkey", "secret")
    with patch("claude_dingtalk_bridge.dingtalk.requests.post") as post:
        post.side_effect = [_token_response(), _ok_response()]
        transport.send_text("staff-9", "hello")

    token_call = post.call_args_list[0]
    assert token_call.args[0].endswith("/oauth2/accessToken")
    assert token_call.kwargs["json"] == {"appKey": "appkey", "appSecret": "secret"}

    send_call = post.call_args_list[1]
    assert send_call.args[0].endswith("/robot/oToMessages/batchSend")
    assert send_call.kwargs["headers"]["x-acs-dingtalk-access-token"] == "tok-1"
    body = send_call.kwargs["json"]
    assert body["robotCode"] == "appkey"
    assert body["userIds"] == ["staff-9"]
    assert body["msgKey"] == "sampleText"
    assert json.loads(body["msgParam"]) == {"content": "hello"}


def test_token_is_cached_across_sends():
    transport = DingTalkTransport("appkey", "secret")
    with patch("claude_dingtalk_bridge.dingtalk.requests.post") as post:
        post.side_effect = [_token_response(), _ok_response(), _ok_response()]
        transport.send_text("staff-9", "one")
        transport.send_text("staff-9", "two")

    assert post.call_count == 3
    assert post.call_args_list[0].args[0].endswith("/oauth2/accessToken")
    assert post.call_args_list[1].args[0].endswith("/batchSend")
    assert post.call_args_list[2].args[0].endswith("/batchSend")


def _unauthorized_response():
    resp = MagicMock()
    resp.status_code = 401
    return resp


def test_send_retries_once_on_401_with_fresh_token():
    transport = DingTalkTransport("appkey", "secret")
    second_token = MagicMock()
    second_token.json.return_value = {"accessToken": "tok-2", "expireIn": 7200}
    second_token.raise_for_status.return_value = None
    with patch("claude_dingtalk_bridge.dingtalk.requests.post") as post:
        post.side_effect = [
            _token_response(),       # initial token fetch
            _unauthorized_response(),  # first send rejects stale token
            second_token,             # token refetch after 401
            _ok_response(),           # retry succeeds
        ]
        transport.send_text("staff-9", "hi")

    # 4 calls total: token, 401 send, fresh token, ok send.
    assert post.call_count == 4
    assert post.call_args_list[1].args[0].endswith("/batchSend")
    assert post.call_args_list[2].args[0].endswith("/accessToken")
    assert post.call_args_list[3].kwargs["headers"]["x-acs-dingtalk-access-token"] == "tok-2"


def test_send_does_not_retry_more_than_once():
    transport = DingTalkTransport("appkey", "secret")
    second_token = MagicMock()
    second_token.json.return_value = {"accessToken": "tok-2", "expireIn": 7200}
    second_token.raise_for_status.return_value = None
    second_unauth = _unauthorized_response()
    second_unauth.raise_for_status.side_effect = Exception("401 still")
    with patch("claude_dingtalk_bridge.dingtalk.requests.post") as post:
        post.side_effect = [
            _token_response(),
            _unauthorized_response(),
            second_token,
            second_unauth,
        ]
        try:
            transport.send_text("staff-9", "hi")
            assert False, "expected exception on persistent 401"
        except Exception:
            pass
    # Two send attempts, then we stop hammering.
    assert post.call_count == 4


def test_stale_token_within_margin_triggers_refetch():
    # token is present but expires within the refresh margin — the cache guard
    # (time.time() < expiry - margin) is false, so a new fetch must happen.
    import time
    from claude_dingtalk_bridge.dingtalk import _TOKEN_REFRESH_MARGIN

    transport = DingTalkTransport("appkey", "secret")
    transport._token = "old-tok"
    transport._token_expiry = time.time() + _TOKEN_REFRESH_MARGIN - 1  # inside margin

    fresh_token = MagicMock()
    fresh_token.json.return_value = {"accessToken": "new-tok", "expireIn": 7200}
    fresh_token.raise_for_status.return_value = None

    with patch("claude_dingtalk_bridge.dingtalk.requests.post") as post:
        post.side_effect = [fresh_token, _ok_response()]
        transport.send_text("staff-9", "hello")

    # First call must be a token fetch, not a send with the stale token.
    assert post.call_count == 2
    assert post.call_args_list[0].args[0].endswith("/oauth2/accessToken")
    assert post.call_args_list[1].kwargs["headers"]["x-acs-dingtalk-access-token"] == "new-tok"


def test_posts_bypass_ambient_proxy():
    # DingTalk is reached directly; the daemon's control-plane must not ride
    # the ambient/system proxy (macOS system proxy or http_proxy env), which
    # requests would otherwise honor. Both the token fetch and the send POST
    # must explicitly disable proxies.
    transport = DingTalkTransport("appkey", "secret")
    with patch("claude_dingtalk_bridge.dingtalk.requests.post") as post:
        post.side_effect = [_token_response(), _ok_response()]
        transport.send_text("staff-9", "hi")
    for call in post.call_args_list:
        assert call.kwargs["proxies"] == {"http": None, "https": None}


def test_posts_use_bounded_connect_timeout():
    # A scalar timeout would apply the same ceiling to connect AND read, letting
    # retried connect-timeouts stack into a long thread stall. Every POST must
    # pass the (connect, read) tuple whose connect phase is capped below read.
    from claude_dingtalk_bridge.dingtalk import _POST_TIMEOUT

    connect, read = _POST_TIMEOUT
    assert connect < read
    transport = DingTalkTransport("appkey", "secret")
    with patch("claude_dingtalk_bridge.dingtalk.requests.post") as post:
        post.side_effect = [_token_response(), _ok_response()]
        transport.send_text("staff-9", "hi")
    for call in post.call_args_list:
        assert call.kwargs["timeout"] == _POST_TIMEOUT


def test_send_retries_on_connect_timeout_then_succeeds():
    # A ConnectTimeout proves the TCP handshake never completed — the request
    # never reached DingTalk, so one retry safely absorbs the blip.
    transport = DingTalkTransport("appkey", "secret")
    with patch("claude_dingtalk_bridge.dingtalk.requests.post") as post, \
         patch("claude_dingtalk_bridge.dingtalk.time.sleep") as sleep:
        post.side_effect = [
            requests.exceptions.ConnectTimeout("handshake stalled"),
            _token_response(),
            _ok_response(),
        ]
        transport.send_text("staff-9", "hi")
    # token (blip → retry → ok) + send = 3 posts, one backoff sleep.
    assert post.call_count == 3
    assert sleep.call_count == 1


def test_connect_timeout_retries_are_bounded_then_raise():
    transport = DingTalkTransport("appkey", "secret")
    with patch("claude_dingtalk_bridge.dingtalk.requests.post") as post, \
         patch("claude_dingtalk_bridge.dingtalk.time.sleep") as sleep:
        post.side_effect = requests.exceptions.ConnectTimeout("down")
        try:
            transport.send_text("staff-9", "hi")
            assert False, "persistent ConnectTimeout must surface"
        except requests.exceptions.ConnectTimeout:
            pass
    # 3 attempts on the token POST, then give up; 2 backoffs between them.
    assert post.call_count == 3
    assert sleep.call_count == 2


def test_connection_error_is_not_retried():
    # A bare ConnectionError (peer RST, RemoteDisconnected on a reused
    # keep-alive, a reset mid-response) is ambiguous — the send may already have
    # landed. Retrying could double a notice, so it must propagate at once, just
    # like ReadTimeout. Only ConnectTimeout (provably undelivered) is retried.
    transport = DingTalkTransport("appkey", "secret")
    with patch("claude_dingtalk_bridge.dingtalk.requests.post") as post, \
         patch("claude_dingtalk_bridge.dingtalk.time.sleep") as sleep:
        post.side_effect = requests.exceptions.ConnectionError("connection reset")
        try:
            transport.send_text("staff-9", "hi")
            assert False, "ambiguous ConnectionError must propagate"
        except requests.exceptions.ConnectionError:
            pass
    assert post.call_count == 1
    assert sleep.call_count == 0


def test_read_timeout_is_not_retried():
    # ReadTimeout means the request may already have been delivered — resending
    # could double a notice, so it must propagate immediately, not retry.
    transport = DingTalkTransport("appkey", "secret")
    with patch("claude_dingtalk_bridge.dingtalk.requests.post") as post, \
         patch("claude_dingtalk_bridge.dingtalk.time.sleep") as sleep:
        post.side_effect = requests.exceptions.ReadTimeout("slow")
        try:
            transport.send_text("staff-9", "hi")
            assert False, "ReadTimeout must propagate"
        except requests.exceptions.ReadTimeout:
            pass
    assert post.call_count == 1
    assert sleep.call_count == 0


def test_stale_fetch_does_not_clobber_fresher_cached_token():
    # _access_token fetches outside the lock, so a slow fetch can land its store
    # AFTER a concurrent 401-refetch already cached a fresher token. The store
    # must keep the newer token (larger expiry stamp) rather than overwrite it
    # with the stale one — otherwise the next send picks up a revoked token and
    # eats a needless 401.
    import time

    transport = DingTalkTransport("appkey", "secret")

    def slow_fetch_returns_stale(url, **kwargs):
        # Simulate a concurrent refetch landing a fresher token in the cache
        # while THIS (slower) fetch is still in flight.
        transport._token = "fresh-tok"
        transport._token_expiry = time.time() + 7200
        resp = MagicMock()
        resp.json.return_value = {"accessToken": "stale-tok", "expireIn": 1}
        resp.raise_for_status.return_value = None
        return resp

    with patch(
        "claude_dingtalk_bridge.dingtalk.requests.post",
        side_effect=slow_fetch_returns_stale,
    ):
        token = transport._access_token()

    assert token == "fresh-tok"
    assert transport._token == "fresh-tok"


def test_send_markdown_uses_markdown_key():
    transport = DingTalkTransport("appkey", "secret")
    with patch("claude_dingtalk_bridge.dingtalk.requests.post") as post:
        post.side_effect = [_token_response(), _ok_response()]
        transport.send_markdown("staff-9", "title", "## body")

    body = post.call_args_list[1].kwargs["json"]
    assert body["msgKey"] == "sampleMarkdown"
    assert json.loads(body["msgParam"]) == {"title": "title", "text": "## body"}
