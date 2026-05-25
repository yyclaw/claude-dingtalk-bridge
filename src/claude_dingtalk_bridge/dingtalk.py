from __future__ import annotations

import json
import threading
import time

import requests

_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
_SEND_URL = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
_TOKEN_REFRESH_MARGIN = 300  # refresh 5 minutes early


class DingTalkTransport:
    """Sends proactive 1:1 messages to a DingTalk user via the Open API."""

    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._lock = threading.Lock()

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expiry - _TOKEN_REFRESH_MARGIN:
                return self._token
            resp = requests.post(
                _TOKEN_URL,
                json={"appKey": self._client_id, "appSecret": self._client_secret},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = str(data["accessToken"])
            self._token_expiry = time.time() + int(data.get("expireIn", 7200))
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
        for attempt in (1, 2):
            headers = {
                "Content-Type": "application/json",
                "x-acs-dingtalk-access-token": self._access_token(),
            }
            resp = requests.post(_SEND_URL, headers=headers, json=body, timeout=10)
            if resp.status_code == 401 and attempt == 1:
                self._invalidate_token()
                continue
            resp.raise_for_status()
            return

    def send_text(self, user_id: str, text: str) -> None:
        self._send(user_id, "sampleText", {"content": text})

    def send_markdown(self, user_id: str, title: str, text: str) -> None:
        self._send(user_id, "sampleMarkdown", {"title": title, "text": text})
