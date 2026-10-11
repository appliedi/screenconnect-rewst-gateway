from __future__ import annotations

import hmac
import ipaddress
import math
import time
from collections import deque
from collections.abc import Callable
from typing import Literal

from .config import Settings

KeyId = Literal["read", "write"]
READ_METHODS = frozenset({"GET", "HEAD"})


def identify_key(settings: Settings, presented: str | None) -> KeyId | None:
    """Constant-time compare against both keys; both comparisons always run."""
    candidate = (presented or "").encode()
    matches: dict[KeyId, bool] = {}
    for key_id, configured in (("read", settings.read_key), ("write", settings.write_key)):
        matches[key_id] = bool(configured) and hmac.compare_digest(candidate, configured.encode())
    if matches["write"]:
        return "write"
    if matches["read"]:
        return "read"
    return None


def source_ip_allowed(settings: Settings, host: str | None) -> bool:
    if not settings.allowed_source_ips:
        return True
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in settings.allowed_source_ips)


class RateLimiter:
    """Sliding-window limiter, per (key id, read|write)."""

    def __init__(
        self,
        limits: dict[str, int],
        *,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._limits = limits
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = {}

    def hit(self, key_id: str, kind: str) -> int | None:
        """Record a request. Returns seconds to wait if over the limit, else None."""
        now = self._clock()
        hits = self._hits.setdefault((key_id, kind), deque())
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if len(hits) >= self._limits[kind]:
            return max(1, math.ceil(hits[0] + self._window - now))
        hits.append(now)
        return None
