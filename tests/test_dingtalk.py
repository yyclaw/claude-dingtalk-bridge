import json
from unittest.mock import MagicMock, patch

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


def test_send_markdown_uses_markdown_key():
    transport = DingTalkTransport("appkey", "secret")
    with patch("claude_dingtalk_bridge.dingtalk.requests.post") as post:
        post.side_effect = [_token_response(), _ok_response()]
        transport.send_markdown("staff-9", "title", "## body")

    body = post.call_args_list[1].kwargs["json"]
    assert body["msgKey"] == "sampleMarkdown"
    assert json.loads(body["msgParam"]) == {"title": "title", "text": "## body"}
