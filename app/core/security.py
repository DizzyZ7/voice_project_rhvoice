from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque

from fastapi import Header, HTTPException, Request, status


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


API_TOKEN = os.environ.get("VOICE_API_TOKEN", "dev-token-change-me")
AUTH_DISABLED = _truthy(os.environ.get("VOICE_AUTH_DISABLED"))


def extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    prefix = "bearer "
    if authorization.lower().startswith(prefix):
        return authorization[len(prefix) :].strip()
    return None


def require_api_token(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    if AUTH_DISABLED:
        return
    provided = extract_bearer_token(authorization) or x_api_key
    if not provided or provided != API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


@dataclass
class RateLimitConfig:
    requests: int
    window_seconds: int


class InMemoryRateLimiter:
    def __init__(self, config: RateLimitConfig):
        self.config = config
        self._buckets: dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.config.window_seconds
        with self._lock:
            bucket = self._buckets.setdefault(key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.config.requests:
                return False
            bucket.append(now)
            return True


def request_client_key(request: Request | None) -> str:
    if request is None or request.client is None:
        return "unknown"
    host = request.client.host or "unknown"
    return host
