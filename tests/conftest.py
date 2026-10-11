from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.allowlist import NATIVE_METHODS, RESTFUL_METHODS
from gateway.config import Settings
from gateway.main import create_app

BASE_URL = "https://sc.example.com"
READ_KEY = "read-key-0123456789"
WRITE_KEY = "write-key-0123456789"
RESTFUL_SECRET = "restful-secret-abcdef"
PASSWORD = "sc-password-xyz123"
ANTI_FORGERY = "anti-forgery-token-123456"
SESSION_ID = "11111111-2222-3333-4444-555555555555"
FIXED_NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)

Handler = Callable[[Any], Any]


def raw_session(
    session_id: str = SESSION_ID,
    name: str = "PC-01",
    properties: list[str] | None = None,
    session_type: int = 2,
    online: bool = False,
) -> dict[str, Any]:
    return {
        "SessionID": session_id,
        "Name": name,
        "SessionType": session_type,
        "GuestMachineName": name,
        "GuestLoggedOnUserName": "user",
        "GuestOperatingSystemName": "Windows 11",
        "CustomPropertyValues": properties if properties is not None else ["Acme", "HQ", "IT", "Laptop", "", "", "", ""],
        "ActiveConnections": [{"ProcessType": 2}] if online else [{"ProcessType": 1}],
    }


def live_data(sessions: list[dict] | None = None, summaries: list[list[dict]] | None = None) -> dict:
    return {
        "ResponseInfoMap": {
            "HostSessionInfo": {"Sessions": sessions or [], "PathSessionGroupSummaries": summaries or []}
        }
    }


class FakeScreenConnect:
    """In-memory ScreenConnect that serves both upstream APIs and records every call."""

    def __init__(self) -> None:
        self.restful: dict[str, Handler] = {}
        self.native: dict[str, Handler] = {}
        self.calls: list[tuple[str, str, Any, httpx.Request]] = []
        self.logins: list[list[Any]] = []
        self.valid_cookie: str | None = None
        self.expire_next_native_call = False

    def _respond(self, handler: Handler | None, args: Any) -> httpx.Response:
        if handler is None:
            return httpx.Response(500, json={"error": "no fake handler"})
        result = handler(args)
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    def handle_restful(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        args = json.loads(request.content or b"null")
        self.calls.append(("restful", method, args, request))
        assert method in RESTFUL_METHODS, f"non-allowlisted restful call {method}"
        return self._respond(self.restful.get(method), args)

    def handle_native(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/Services/AuthenticationService.ashx/TryLogin":
            self.logins.append(json.loads(request.content))
            self.valid_cookie = f"auth-cookie-{len(self.logins)}-abcdef"
            return httpx.Response(200, json=True, headers={"set-cookie": f".ASPXAUTH={self.valid_cookie}; Path=/"})
        if path == "/" and request.method == "GET":
            html = f'<script>SC.context = {{"antiForgeryToken":"{ANTI_FORGERY}","x":1}};</script>'
            return httpx.Response(200, text=html)

        method = path.removeprefix("/Services/")
        args = json.loads(request.content or b"null")
        self.calls.append(("native", method, args, request))
        assert method in NATIVE_METHODS, f"non-allowlisted native call {method}"
        cookie_ok = self.valid_cookie and f".ASPXAUTH={self.valid_cookie}" in request.headers.get("cookie", "")
        if self.expire_next_native_call:
            self.expire_next_native_call = False
            self.valid_cookie = None
            return httpx.Response(403)
        if not cookie_ok or request.headers.get("x-anti-forgery-token") != ANTI_FORGERY:
            return httpx.Response(403)
        return self._respond(self.native.get(method), args)

    def calls_to(self, backend: str, method: str) -> list[Any]:
        return [args for b, m, args, _ in self.calls if b == backend and m == method]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_settings(**overrides: Any) -> Settings:
    base = Settings(
        sc_base_url=BASE_URL,
        restful_secret=RESTFUL_SECRET,
        sc_username="svc-gateway",
        sc_password=PASSWORD,
        read_key=READ_KEY,
        write_key=WRITE_KEY,
    )
    return replace(base, **overrides)


@pytest.fixture
def fake() -> FakeScreenConnect:
    return FakeScreenConnect()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def make_client(fake: FakeScreenConnect, clock: Clock) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def factory(client_addr: tuple[str, int] = ("127.0.0.1", 50000), **overrides: Any) -> TestClient:
        app = create_app(
            make_settings(**overrides),
            restful_transport=httpx.MockTransport(fake.handle_restful),
            native_transport=httpx.MockTransport(fake.handle_native),
            clock=clock,
            wall_clock=lambda: FIXED_NOW,
        )
        client = TestClient(app, client=client_addr, raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


READ = {"X-Api-Key": READ_KEY}
WRITE = {"X-Api-Key": WRITE_KEY}
