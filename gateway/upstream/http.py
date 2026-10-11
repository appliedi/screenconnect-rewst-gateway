"""Shared upstream HTTP policy: error mapping and the single read retry."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..errors import (
    RETRYABLE_READ_ERRORS,
    UpstreamAuthError,
    UpstreamConnectError,
    UpstreamMissingMethodError,
    UpstreamRejectedError,
    UpstreamServerError,
    UpstreamTimeoutError,
)

logger = logging.getLogger("gateway.upstream")


def check_status(response: httpx.Response, backend: str) -> httpx.Response:
    status = response.status_code
    # Redirects aren't followed, so a 3xx means nothing ran (usually a login redirect).
    if status in (401, 403) or 300 <= status < 400:
        raise UpstreamAuthError(f"{backend} upstream rejected the credentials (HTTP {status})", backend=backend)
    if status in (404, 405):
        raise UpstreamMissingMethodError(f"{backend} upstream method not found (HTTP {status})", backend=backend)
    if 400 <= status < 500:
        raise UpstreamRejectedError(f"{backend} upstream rejected the request (HTTP {status})", backend=backend)
    if status >= 500:
        raise UpstreamServerError(f"{backend} upstream error (HTTP {status})", backend=backend)
    return response


async def guarded(send: Callable[[], Awaitable[httpx.Response]], backend: str) -> httpx.Response:
    """Translate transport exceptions into gateway errors."""
    try:
        return await send()
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise UpstreamConnectError(f"cannot connect to {backend} upstream", backend=backend) from exc
    except httpx.TimeoutException as exc:
        raise UpstreamTimeoutError(f"{backend} upstream timed out", backend=backend) from exc
    except httpx.TransportError as exc:
        raise UpstreamServerError(f"{backend} upstream connection failed", backend=backend) from exc


async def send_with_policy(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    backend: str,
    idempotent: bool,
) -> httpx.Response:
    """Reads get one retry on 5xx/timeout; writes are never auto-retried."""
    attempts = 2 if idempotent else 1
    for attempt in range(1, attempts + 1):
        try:
            response = await guarded(send, backend)
            return check_status(response, backend)
        except RETRYABLE_READ_ERRORS as exc:
            if attempt == attempts:
                raise
            logger.warning("%s read failed (%s); retrying once", backend, type(exc).__name__)
    raise AssertionError("unreachable")


def parse_json(response: httpx.Response, backend: str) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise UpstreamServerError(f"{backend} upstream returned invalid JSON", backend=backend) from exc
