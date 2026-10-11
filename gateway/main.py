from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, FastAPI, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import models
from .config import Settings
from .errors import AuthenticationError, ForbiddenError, GatewayError, RateLimitedError
from .logs import audit_write, configure_logging, install_redaction, secret_registry
from .security import READ_METHODS, RateLimiter, identify_key, source_ip_allowed
from .service import Gateway, Served
from .upstream.native import NativeClient
from .upstream.restful import RestfulClient

__version__ = "1.0.0"
logger = logging.getLogger("gateway")

BACKEND_HEADER = "X-Gateway-Backend"
OPEN_PATHS = {"/health"}
READ_LIMIT_PER_MINUTE = 120
WRITE_LIMIT_PER_MINUTE = 10

BackendParam = Annotated[
    Literal["restful", "native"] | None,
    Query(description="Force a specific upstream backend (disables fallback). Normally omitted."),
]
SessionIdParam = Annotated[str, Path(alias="sessionId", pattern=r"^[0-9a-fA-F-]{32,36}$")]
GroupParam = Annotated[
    str | None, Query(description="Session group used by the native backend (default 'All Machines')")
]


def served(response: Response, result: Served[Any]) -> Any:
    response.headers[BACKEND_HEADER] = result.backend
    return result.value


def create_app(
    settings: Settings,
    *,
    restful_transport: httpx.AsyncBaseTransport | None = None,
    native_transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    install_redaction()
    secret_registry.add(*settings.secrets)

    restful = RestfulClient(
        settings.sc_base_url,
        settings.restful_secret,
        timeout=settings.upstream_timeout_seconds,
        transport=restful_transport,
    )
    native = NativeClient(
        settings.sc_base_url,
        settings.sc_username,
        settings.sc_password,
        settings.sc_totp_secret,
        timeout=settings.upstream_timeout_seconds,
        transport=native_transport,
    )
    gateway = Gateway(settings, restful, native, clock=clock)
    limiter = RateLimiter(
        {"read": READ_LIMIT_PER_MINUTE, "write": WRITE_LIMIT_PER_MINUTE}, clock=clock
    )
    health_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        for backend, enabled in (("restful", restful.enabled), ("native", native.enabled)):
            if not enabled:
                logger.warning("%s backend disabled: credentials not configured", backend)
        yield
        await gateway.aclose()

    app = FastAPI(
        title="ScreenConnect Gateway",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.gateway = gateway

    # ---------- errors: always {"message": ...} ----------

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
        headers = {BACKEND_HEADER: exc.backend or "none"}
        if isinstance(exc, RateLimitedError):
            headers["Retry-After"] = str(exc.retry_after)
        if exc.status_code >= 500:
            logger.warning("request failed: %s (%s)", exc.message, type(exc).__name__)
        return JSONResponse({"message": exc.message}, status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo submitted values (they may contain passwords or commands).
        problems = [
            f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg', 'invalid')}"
            for error in exc.errors()
        ]
        return JSONResponse({"message": "invalid request: " + "; ".join(problems)}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse({"message": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled error: %s", type(exc).__name__)
        return JSONResponse({"message": "internal server error"}, status_code=500)

    # ---------- middleware: source IP, backend header, audit ----------

    @app.middleware("http")
    async def gateway_middleware(request: Request, call_next):
        is_write = request.method not in READ_METHODS
        host = request.client.host if request.client else None
        if request.url.path not in OPEN_PATHS and not source_ip_allowed(settings, host):
            response = JSONResponse({"message": "source IP not allowed"}, status_code=403)
        else:
            try:
                response = await call_next(request)
            except Exception:
                if is_write:
                    _audit(request, 500, None)
                raise
        response.headers.setdefault(BACKEND_HEADER, "none")
        if is_write:
            _audit(request, response.status_code, response.headers.get(BACKEND_HEADER))
        return response

    def _audit(request: Request, status_code: int, backend: str | None) -> None:
        route = request.scope.get("route")
        audit_write(
            endpoint=f"{request.method} {getattr(route, 'path', request.url.path)}",
            session_id=request.scope.get("path_params", {}).get("sessionId"),
            key_id=getattr(request.state, "key_id", None),
            backend=None if backend == "none" else backend,
            status_code=status_code,
        )

    # ---------- auth + rate limiting ----------

    def authorize(request: Request) -> None:
        key_id = identify_key(settings, request.headers.get("x-api-key"))
        if key_id is None:
            raise AuthenticationError("missing or invalid X-Api-Key")
        request.state.key_id = key_id
        is_read = request.method in READ_METHODS
        if not is_read and key_id != "write":
            raise ForbiddenError("the read key cannot perform write operations")
        retry_after = limiter.hit(key_id, "read" if is_read else "write")
        if retry_after is not None:
            raise RateLimitedError("rate limit exceeded", retry_after=retry_after)

    # ---------- health ----------

    @app.get("/health", operation_id="health", response_model=models.Health, tags=["health"])
    async def health() -> dict[str, Any]:
        """Probe both backends with a cheap read. Results are cached briefly."""
        cached = health_cache.get("health")
        if cached and cached[0] > clock():
            return cached[1]
        restful_health, native_health = await asyncio.gather(gateway.probe("restful"), gateway.probe("native"))
        results = {"restful": restful_health, "native": native_health}
        enabled = [result for result in results.values() if result["enabled"]]
        healthy = [result for result in enabled if result["ok"]]
        if enabled and len(healthy) == len(enabled):
            status = "ok"
        elif healthy:
            status = "degraded"
        else:
            status = "down"
        body = {"status": status, **results}
        health_cache["health"] = (clock() + settings.health_cache_seconds, body)
        return body

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(authorize)])

    # ---------- reads ----------

    @v1.get("/companies", operation_id="list_companies", response_model=list[models.Company], tags=["companies"])
    async def list_companies(response: Response, backend: BackendParam = None):
        """Distinct companies (Custom Property 1). Use `id` (raw, untrimmed) for lookups."""
        return served(response, await gateway.list_companies(backend))

    @v1.get(
        "/companies/{companyId:path}/sessions",
        operation_id="list_company_sessions",
        response_model=list[models.Session],
        tags=["companies"],
    )
    async def list_company_sessions(
        response: Response,
        company_id: Annotated[str, Path(alias="companyId", description="Company id exactly as returned by /v1/companies")],
        session_type: Annotated[int, Query(alias="sessionType", ge=0, le=2)] = 2,
        backend: BackendParam = None,
    ):
        return served(response, await gateway.list_company_sessions(company_id, session_type, backend))

    @v1.get(
        "/session-groups",
        operation_id="list_session_groups",
        response_model=list[models.SessionGroup],
        tags=["session-groups"],
    )
    async def list_session_groups(response: Response, backend: BackendParam = None):
        return served(response, await gateway.list_session_groups(backend))

    @v1.get(
        "/session-groups/sessions",
        operation_id="list_group_sessions",
        response_model=models.GroupSessions,
        tags=["session-groups"],
    )
    async def list_group_sessions(
        response: Response,
        path: Annotated[list[str], Query(min_length=1, description="Group path segments, outermost first. Repeat the parameter.")],
        session_type: Annotated[int, Query(alias="sessionType", ge=0, le=2)] = 2,
        filter: Annotated[str, Query(description="ScreenConnect session filter expression")] = "",
        limit: Annotated[int, Query(ge=1, le=10_000)] = 1000,
        backend: BackendParam = None,
    ):
        return served(response, await gateway.list_group_sessions(path, session_type, filter, limit, backend))

    @v1.get("/sessions", operation_id="search_sessions", response_model=list[models.Session], tags=["sessions"])
    async def search_sessions(
        response: Response,
        filter: Annotated[str | None, Query(description="ScreenConnect filter expression")] = None,
        name: Annotated[str | None, Query(description="Exact session name")] = None,
        session_type: Annotated[int | None, Query(alias="sessionType", ge=0, le=2)] = None,
        backend: BackendParam = None,
    ):
        """Provide exactly one of `filter` or `name`."""
        return served(
            response,
            await gateway.search_sessions(
                filter_expression=filter, name=name, session_type=session_type, forced=backend
            ),
        )

    @v1.get("/sessions/{sessionId}", operation_id="get_session", response_model=models.Session, tags=["sessions"])
    async def get_session(response: Response, session_id: SessionIdParam, backend: BackendParam = None):
        return served(response, await gateway.get_session(session_id, backend))

    @v1.get(
        "/sessions/{sessionId}/details",
        operation_id="get_session_details",
        response_model=models.SessionDetails,
        tags=["sessions"],
    )
    async def get_session_details(
        response: Response, session_id: SessionIdParam, group: GroupParam = None, backend: BackendParam = None
    ):
        """Session plus events. Command output arrives as eventType 70."""
        return served(response, await gateway.get_session_details(session_id, group, backend))

    @v1.get(
        "/sessions/{sessionId}/connection-logs",
        operation_id="get_connection_logs",
        response_model=models.ConnectionLogs,
        tags=["sessions"],
    )
    async def get_connection_logs(
        response: Response, session_id: SessionIdParam, group: GroupParam = None, backend: BackendParam = None
    ):
        return served(response, await gateway.get_connection_logs(session_id, group, backend))

    # ---------- writes ----------

    @v1.post(
        "/sessions",
        operation_id="create_session",
        response_model=models.CreatedSession,
        status_code=201,
        tags=["sessions"],
    )
    async def create_session(response: Response, body: models.CreateSessionRequest, backend: BackendParam = None):
        result = await gateway.create_session(
            session_type=body.session_type,
            name=body.name,
            is_public=body.is_public,
            code=body.code,
            changes=body.named_changes(),
            forced=backend,
        )
        return served(response, result)

    @v1.post(
        "/sessions/{sessionId}/commands",
        operation_id="send_command",
        response_model=models.QueuedResult,
        status_code=202,
        tags=["sessions"],
    )
    async def send_command(
        response: Response, session_id: SessionIdParam, body: models.CommandRequest, backend: BackendParam = None
    ):
        """Queue a command. Poll /details for eventType 70 to read the output."""
        result = await gateway.send_command(
            session_id,
            command=body.command,
            shell=body.shell,
            timeout_ms=body.timeout_ms,
            group=body.group,
            forced=backend,
        )
        return served(response, result)

    @v1.post(
        "/sessions/{sessionId}/messages",
        operation_id="send_message",
        response_model=models.QueuedResult,
        status_code=202,
        tags=["sessions"],
    )
    async def send_message(
        response: Response, session_id: SessionIdParam, body: models.MessageRequest, backend: BackendParam = None
    ):
        return served(response, await gateway.send_message(session_id, body.sender, body.message, backend))

    @v1.post(
        "/sessions/{sessionId}/notes",
        operation_id="add_note",
        response_model=models.OkResult,
        tags=["sessions"],
    )
    async def add_note(
        response: Response, session_id: SessionIdParam, body: models.NoteRequest, backend: BackendParam = None
    ):
        return served(response, await gateway.add_note(session_id, body.note, backend))

    @v1.post(
        "/sessions/{sessionId}/toolbox",
        operation_id="run_toolbox_item",
        response_model=models.QueuedResult,
        status_code=202,
        tags=["sessions"],
    )
    async def run_toolbox_item(
        response: Response, session_id: SessionIdParam, body: models.ToolboxRequest, backend: BackendParam = None
    ):
        return served(response, await gateway.run_toolbox_item(session_id, body.item_name, backend))

    @v1.patch(
        "/sessions/{sessionId}/name",
        operation_id="rename_session",
        response_model=models.RenamedSession,
        tags=["sessions"],
    )
    async def rename_session(
        response: Response, session_id: SessionIdParam, body: models.RenameRequest, backend: BackendParam = None
    ):
        return served(response, await gateway.rename_session(session_id, body.name, backend))

    @v1.patch(
        "/sessions/{sessionId}/custom-properties",
        operation_id="update_custom_properties",
        response_model=models.CustomPropertiesResult,
        tags=["sessions"],
    )
    async def update_custom_properties(
        response: Response,
        session_id: SessionIdParam,
        body: models.CustomPropertiesUpdate,
        backend: BackendParam = None,
    ):
        """Merge the supplied fields into the current 8 values; returns the resulting 8 values."""
        return served(response, await gateway.update_custom_properties(session_id, body.changes(), backend))

    @v1.post(
        "/users",
        operation_id="create_user",
        response_model=models.CreatedUser,
        status_code=201,
        tags=["users"],
    )
    async def create_user(response: Response, body: models.CreateUserRequest, backend: BackendParam = None):
        result = await gateway.create_user(
            user_name=body.user_name,
            password=body.password.get_secret_value(),
            display_name=body.display_name,
            email=body.email,
            roles=body.roles,
            force_password_change=body.force_password_change,
            forced=backend,
        )
        return served(response, result)

    app.include_router(v1)
    app.openapi = lambda: build_openapi(app)  # type: ignore[method-assign]
    return app


ERROR_STATUSES = {
    "400": "Invalid request",
    "401": "Missing or invalid API key",
    "403": "Forbidden (read key on a write, or source IP not allowed)",
    "404": "Not found",
    "429": "Rate limit exceeded",
    "502": "Upstream ScreenConnect error",
    "503": "Required backend unavailable or not configured",
    "504": "Upstream ScreenConnect timeout",
}


def build_openapi(app: FastAPI) -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version="3.1.0",
        description=(
            "Single REST API over ConnectWise ScreenConnect's built-in web API and the "
            "RESTful API Manager extension. All reads are GET. Responses carry "
            "`X-Gateway-Backend: restful|native`. Errors are always `{\"message\": \"...\"}`."
        ),
        routes=app.routes,
        servers=[{"url": "https://{hostname}", "variables": {"hostname": {"default": "example.com"}}}],
    )
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)
    schemas["ErrorMessage"] = models.ErrorMessage.model_json_schema()
    components["securitySchemes"] = {"ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}
    schema["security"] = [{"ApiKeyAuth": []}]

    error_ref = {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorMessage"}}}}
    for path, item in schema["paths"].items():
        for operation in item.values():
            responses = operation.setdefault("responses", {})
            responses.pop("422", None)
            if path in OPEN_PATHS:
                operation["security"] = []
                continue
            for status, description in ERROR_STATUSES.items():
                responses.setdefault(status, {"description": description, **error_ref})
    app.openapi_schema = schema
    return schema


def create_app_from_env() -> FastAPI:
    configure_logging()
    return create_app(Settings.from_env())
