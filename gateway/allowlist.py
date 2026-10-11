"""The only upstream methods the gateway is permitted to call.

Native login (AuthenticationService.ashx/TryLogin + GET / for the anti-forgery token)
is handled separately inside the native client and is not callable through call().
"""

from __future__ import annotations

from .errors import UpstreamNotAllowedError

RESTFUL_METHODS = frozenset(
    {
        "GetSessionsByFilter",
        "GetSessionsByName",
        "GetSessionBySessionID",
        "GetSessionDetailsBySessionID",
        "CreateSession",
        "SendCommandToSession",
        "SendMessageToSession",
        "AddNoteToSession",
        "SendToolboxItemToSession",
        "UpdateSessionName",
        "UpdateSessionCustomProperties",
    }
)

NATIVE_METHODS = frozenset(
    {
        "PageService.ashx/GetLiveData",
        "PageService.ashx/GetSessionDetails",
        "PageService.ashx/AddSessionEvents",
        "PageService.ashx/UpdateSessionCustomPropertyValue",
        "SessionGroupService.ashx/GetSessionGroups",
        "SecurityService.ashx/SaveUser",
    }
)

ALLOWED_METHODS = {"restful": RESTFUL_METHODS, "native": NATIVE_METHODS}


def ensure_allowed(backend: str, method: str) -> None:
    if method not in ALLOWED_METHODS.get(backend, frozenset()):
        raise UpstreamNotAllowedError(
            f"upstream method {backend}:{method} is not allowlisted", backend=backend
        )
