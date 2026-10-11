"""ConnectWise Labs RESTful API Manager extension client."""

from __future__ import annotations

from typing import Any

import httpx

from ..allowlist import ensure_allowed
from ..errors import BackendUnavailableError
from .http import parse_json, send_with_policy

EXTENSION_PATH = "/App_Extensions/2d558935-686a-4bd0-9991-07539f5fe749/Service.ashx"


class RestfulClient:
    name = "restful"

    def __init__(
        self,
        base_url: str,
        secret: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.enabled = bool(base_url and secret)
        self._secret = secret
        self._client = (
            httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport, follow_redirects=False)
            if self.enabled
            else None
        )

    async def call(self, method: str, args: list[Any], *, idempotent: bool) -> Any:
        if self._client is None:
            raise BackendUnavailableError("restful backend is not configured", backend=self.name)
        ensure_allowed(self.name, method)
        client = self._client

        async def send() -> httpx.Response:
            # Positional JSON array body; deliberately no Origin header.
            return await client.post(
                f"{EXTENSION_PATH}/{method}",
                json=args,
                headers={"CTRLAuthHeader": self._secret},
            )

        response = await send_with_policy(send, backend=self.name, idempotent=idempotent)
        return parse_json(response, self.name)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
