"""ScreenConnect built-in web API (/Services/*.ashx) client with cookie login."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
import pyotp

from ..allowlist import ensure_allowed
from ..errors import BackendUnavailableError, UpstreamAuthError
from ..logs import secret_registry
from .http import check_status, guarded, parse_json, send_with_policy

logger = logging.getLogger("gateway.native")

LOGIN_PATH = "/Services/AuthenticationService.ashx/TryLogin"
LOGIN_FAILURE_COOLDOWN_SECONDS = 60.0
# A 403 this soon after a fresh login is a permission problem, not an expired session.
FRESH_LOGIN_SECONDS = 30.0
_ANTI_FORGERY_RE = re.compile(r"""["']?antiForgeryToken["']?\s*[:=]\s*["']([^"']+)["']""")


def extract_anti_forgery_token(html: str) -> str | None:
    match = _ANTI_FORGERY_RE.search(html)
    return match.group(1) if match else None


class NativeClient:
    name = "native"

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        totp_secret: str = "",
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.enabled = bool(base_url and username and password)
        self._username = username
        self._password = password
        self._totp = pyotp.TOTP(totp_secret) if totp_secret else None
        parts = urlsplit(base_url)
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._client = (
            httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport, follow_redirects=False)
            if self.enabled
            else None
        )
        self._anti_forgery_token: str | None = None
        self._generation = 0
        self._login_lock = asyncio.Lock()
        self._clock = clock
        self._login_blocked_until = 0.0
        self._last_login_at: float | None = None

    async def call(self, method: str, args: list[Any], *, idempotent: bool) -> Any:
        if self._client is None:
            raise BackendUnavailableError("native backend is not configured", backend=self.name)
        ensure_allowed(self.name, method)
        client = self._client

        if self._anti_forgery_token is None:
            await self._login(stale_generation=self._generation)

        for auth_attempt in (1, 2):
            generation = self._generation
            token = self._anti_forgery_token or ""

            async def send() -> httpx.Response:
                return await client.post(
                    f"/Services/{method}",
                    json=args,
                    headers={"x-anti-forgery-token": token, "Origin": self._origin},
                )

            try:
                response = await send_with_policy(
                    send, backend=self.name, idempotent=idempotent
                )
                return parse_json(response, self.name)
            except UpstreamAuthError:
                # 401/403 means the call was not executed, so a single re-login + retry
                # is safe even for writes. Skip it right after a login: re-logging in
                # would not help and could reuse the same TOTP code.
                fresh = self._last_login_at is not None and self._clock() - self._last_login_at < FRESH_LOGIN_SECONDS
                if auth_attempt == 2 or fresh:
                    raise
                logger.info("native session rejected; re-authenticating")
                await self._login(stale_generation=generation)
        raise AssertionError("unreachable")

    async def _login(self, *, stale_generation: int) -> None:
        """Single-flight login: concurrent callers wait, then reuse the fresh session."""
        assert self._client is not None
        client = self._client
        async with self._login_lock:
            if self._anti_forgery_token is not None and self._generation != stale_generation:
                return
            # Don't hammer TryLogin with bad credentials: that locks the service account.
            if self._clock() < self._login_blocked_until:
                raise UpstreamAuthError("native login failed recently; waiting before retrying", backend=self.name)
            try:
                await self._attempt_login(client)
            except UpstreamAuthError:
                self._login_blocked_until = self._clock() + LOGIN_FAILURE_COOLDOWN_SECONDS
                raise

    async def _attempt_login(self, client: httpx.AsyncClient) -> None:
        self._anti_forgery_token = None
        client.cookies.clear()
        code = self._totp.now() if self._totp else ""
        headers = {"Origin": self._origin}

        login = await guarded(
            lambda: client.post(
                LOGIN_PATH,
                json=[self._username, self._password, code, True, secrets.token_hex(16)],
                headers=headers,
            ),
            self.name,
        )
        if login.status_code >= 300:
            raise UpstreamAuthError(f"native login failed (HTTP {login.status_code})", backend=self.name)
        self._register_cookie_secrets()

        page = check_status(await guarded(lambda: client.get("/", headers=headers), self.name), self.name)
        token = extract_anti_forgery_token(page.text)
        if not token:
            raise UpstreamAuthError(
                "native login failed: anti-forgery token not found (check credentials/TOTP)",
                backend=self.name,
            )
        secret_registry.add(token)
        self._register_cookie_secrets()
        self._anti_forgery_token = token
        self._generation += 1
        self._last_login_at = self._clock()
        logger.info("native login succeeded")

    def _register_cookie_secrets(self) -> None:
        assert self._client is not None
        secret_registry.add(*(cookie.value for cookie in self._client.cookies.jar))

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
