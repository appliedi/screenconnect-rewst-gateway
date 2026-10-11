from __future__ import annotations


class GatewayError(Exception):
    """Base error. Every subclass maps to an HTTP status and a {"message": ...} body.

    fallback_ok: a read may be retried on the other backend.
    write_safe:  the upstream definitely did NOT apply the request, so a write may
                 also be retried on the other backend without risking a double apply.
    """

    status_code = 500
    fallback_ok = False
    write_safe = False

    def __init__(self, message: str, *, backend: str | None = None):
        super().__init__(message)
        self.message = message
        self.backend = backend


class BadRequestError(GatewayError):
    status_code = 400


class AuthenticationError(GatewayError):
    status_code = 401


class ForbiddenError(GatewayError):
    status_code = 403


class NotFoundError(GatewayError):
    status_code = 404


class RateLimitedError(GatewayError):
    status_code = 429

    def __init__(self, message: str, *, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


class BackendUnavailableError(GatewayError):
    """Backend is disabled (missing credentials) or not usable at all."""

    status_code = 503
    fallback_ok = True
    write_safe = True


class UpstreamAuthError(GatewayError):
    status_code = 502
    fallback_ok = True
    write_safe = True


class UpstreamConnectError(GatewayError):
    """Could not establish a connection: the request never reached ScreenConnect."""

    status_code = 502
    fallback_ok = True
    write_safe = True


class UpstreamMissingMethodError(GatewayError):
    """Upstream 404/405 for a method we call: extension missing or endpoint moved."""

    status_code = 502
    fallback_ok = True
    write_safe = True


class UpstreamTimeoutError(GatewayError):
    status_code = 504
    fallback_ok = True


class UpstreamServerError(GatewayError):
    status_code = 502
    fallback_ok = True


class UpstreamRejectedError(GatewayError):
    """Upstream 4xx caused by the request content. Never falls back."""

    status_code = 400


class UpstreamNotAllowedError(GatewayError):
    """The gateway tried to call an upstream method outside the allowlist (a bug)."""

    status_code = 500


RETRYABLE_READ_ERRORS = (UpstreamTimeoutError, UpstreamServerError, UpstreamConnectError)
