"""WEEX V3 request signing.

Rule (spot and futures, checked against WEEX docs August 2026):

    timestamp + METHOD + requestPath [+ ?query] [+ body]

HMAC-SHA256 with the secret key, then Base64-encode the digest.

Sign the exact bytes you transmit. Compact JSON (no spaces) is required if
the HTTP client would otherwise re-serialize the body.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any


CLIENT_ORDER_ID_PATTERN = r"^[.A-Z:/a-z0-9_-]{1,36}$"


def compact_json(payload: dict[str, Any]) -> str:
    """Serialize a body the same way we sign and send it."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def build_sign_message(
    timestamp: str,
    method: str,
    request_path: str,
    query: str = "",
    body: str = "",
) -> str:
    """Build the pre-hash string. `query` is without a leading '?'."""
    message = f"{timestamp}{method.upper()}{request_path}"
    if query:
        message += f"?{query}"
    if body:
        message += body
    return message


def sign_message(secret: str, message: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def sign_request(
    secret: str,
    timestamp: str,
    method: str,
    request_path: str,
    query: str = "",
    body: str = "",
) -> str:
    return sign_message(secret, build_sign_message(timestamp, method, request_path, query, body))


def ws_private_sign(secret: str, timestamp: str, request_path: str = "/v3/ws/private") -> str:
    """Private WS handshake: timestamp + requestPath only (no method, no body)."""
    return sign_message(secret, f"{timestamp}{request_path}")
