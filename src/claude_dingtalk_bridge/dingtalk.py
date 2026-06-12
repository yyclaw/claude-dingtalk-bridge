from __future__ import annotations

import json
import threading
import time

import requests

_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
_SEND_URL = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
_TOKEN_REFRESH_MARGIN = 300  # refresh 5 minutes early
# Backoffs between connect-retry attempts; len + 1 == total attempts.
_RETRY_BACKOFFS = (0.5, 1.0)
# (connect, read) timeout. The connect phase is capped well below the read
# budget so the connect-timeout retries above can't stack into a ~30s thread
# stall (a scalar timeout would apply that same ceiling to every attempt).
_POST_TIMEOUT = (5.0, 10.0)


class DingTalkTransport:
    """Sends proactive 1:1 messages to a DingTalk user via the Open API."""

    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._lock = threading.Lock()

    def _post(self, url: str, **kwargs) -> requests.Response:
        # Retry ONLY on ConnectTimeout — the one failure that proves the TCP
        # handshake never completed, so the request never reached DingTalk and a
        # resend can't duplicate a message. Every other ConnectionError (peer
        # RST, RemoteDisconnected on a reused keep-alive, a reset while the
        # response was in flight) is ambiguous: the send may already have
        # landed, so — like ReadTimeout — it propagates rather than risk
        # doubling a notice.
        # Bypass the ambient/system proxy: requests honors the macOS system
        # proxy (and http_proxy env) by default, but the daemon reaches DingTalk
        # directly — only Claude's task traffic and the geo check ride the geo
        # proxy. Routing control-plane sends through it wedges every message.
        kwargs.setdefault("proxies", {"http": None, "https": None})
        for backoff in (*_RETRY_BACKOFFS, None):
            try:
                return requests.post(url, **kwargs)
            except requests.exceptions.ConnectTimeout:
                if backoff is None:
                    raise
                time.sleep(backoff)
        raise AssertionError("unreachable")  # pragma: no cover

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expiry - _TOKEN_REFRESH_MARGIN:
                return self._token
        # Fetch outside the lock: _post's connection-retry backoffs sleep up to
        # sum(_RETRY_BACKOFFS) seconds, and holding the lock across them would
        # stall any concurrent caller (e.g. the auto-update nudge racing a
        # turn's send). A rare double-fetch when two callers miss the cache at
        # once is fine — the store below keeps the newer token, so a slow fetch
        # can't clobber a fresher one.
        resp = self._post(
            _TOKEN_URL,
            json={"appKey": self._client_id, "appSecret": self._client_secret},
            timeout=_POST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        token = str(data["accessToken"])
        expiry = time.time() + int(data.get("expireIn", 7200))
        with self._lock:
            # Keep whichever fetch carries the larger expiry stamp. expiry is
            # stamped when this POST returns (completion time + expireIn), so the
            # more-recently-completed fetch wins and an older one can't clobber
            # it. _invalidate_token zeroes the expiry, so a real fetch still wins
            # right after a revocation. The ordering is by completion time, not
            # server issue time, so a rarely-delayed stale token that lands last
            # can still win — the next send's 401 retry heals that.
            if expiry > self._token_expiry:
                self._token = token
                self._token_expiry = expiry
                return token
            return self._token

    def _invalidate_token(self) -> None:
        """Drop the cached token so the next _access_token() call refetches."""
        with self._lock:
            self._token = None
            self._token_expiry = 0.0

    def _send(self, user_id: str, msg_key: str, msg_param: dict) -> None:
        body = {
            "robotCode": self._client_id,
            "userIds": [user_id],
            "msgKey": msg_key,
            "msgParam": json.dumps(msg_param, ensure_ascii=False),
        }
        # One transparent retry on 401: DingTalk can revoke a token early
        # (rotation, manual invalidation) and the cached value would otherwise
        # stay stale until natural expiry — wedging every phone-bound message.
        for attempt in (1, 2):  # pragma: no branch
            # Natural-exit arm unreachable: each iteration either `continue`s
            # (401 + first attempt) or `return`s after raise_for_status, so
            # the loop never falls through.
            headers = {
                "Content-Type": "application/json",
                "x-acs-dingtalk-access-token": self._access_token(),
            }
            resp = self._post(_SEND_URL, headers=headers, json=body, timeout=_POST_TIMEOUT)
            if resp.status_code == 401 and attempt == 1:
                self._invalidate_token()
                continue
            resp.raise_for_status()
            return

    def send_text(self, user_id: str, text: str) -> None:
        self._send(user_id, "sampleText", {"content": text})

    def send_markdown(self, user_id: str, title: str, text: str) -> None:
        self._send(user_id, "sampleMarkdown", {"title": title, "text": text})
