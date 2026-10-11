from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, FastAPI, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import models
from .config import Settings
from .errors import (
    AmbiguousCompanyError,
    AuthenticationError,
    BadRequestError,
    ForbiddenError,
    GatewayError,
    RateLimitedError,
)
from .logs import audit_write, configure_logging, install_redaction, secret_registry
from .security import READ_METHODS, RateLimiter, identify_key, source_ip_allowed
from .sc_types import DETAILS_CONNECTIONS, DETAILS_EVENTS, DETAILS_SESSION_INFO
from .service import Gateway, Served
from .upstream.native import NativeClient
from .upstream.restful import RestfulClient

__version__ = "1.4.2"
logger = logging.getLogger("gateway")

BACKEND_HEADER = "X-Gateway-Backend"
RESOLVED_COMPANY_HEADER = "X-Gateway-Resolved-Company"
TRUNCATED_HEADER = "X-Gateway-Truncated"
TOTAL_HEADER = "X-Gateway-Total-Count"
TRUNCATED_HEADER_DOC = {
    "description": "true/false when the upstream reports truncation (native GetLiveData); unknown when it gives "
    "no signal (restful). Never assume a complete fleet unless this is false.",
    "schema": {"type": "string", "enum": ["true", "false", "unknown"]},
}
TOTAL_HEADER_DOC = {
    "description": "Native UntruncatedSessionCount for the queried group, when ScreenConnect reports it",
    "schema": {"type": "integer"},
}
LIST_RESPONSES: dict[int | str, dict[str, Any]] = {200: {"headers": {TRUNCATED_HEADER: TRUNCATED_HEADER_DOC}}}
GROUP_LIST_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"headers": {TRUNCATED_HEADER: TRUNCATED_HEADER_DOC, TOTAL_HEADER: TOTAL_HEADER_DOC}}
}
RESOLVED_HEADER_DOC = {
    "description": "URL-encoded raw company id, present when the given id matched only "
    "case/whitespace-insensitively (e.g. a lowercased Rewst mapping id)",
    "schema": {"type": "string"},
}
AMBIGUOUS_RESPONSE = {"model": models.AmbiguousCompany, "description": "Company id matches more than one company"}
COMPANY_RESOLUTION_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"headers": {TRUNCATED_HEADER: TRUNCATED_HEADER_DOC, RESOLVED_COMPANY_HEADER: RESOLVED_HEADER_DOC}},
    409: AMBIGUOUS_RESPONSE,
}
GROUP_RESOLUTION_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "headers": {
            TRUNCATED_HEADER: TRUNCATED_HEADER_DOC,
            TOTAL_HEADER: TOTAL_HEADER_DOC,
            RESOLVED_COMPANY_HEADER: RESOLVED_HEADER_DOC,
        }
    },
    409: AMBIGUOUS_RESPONSE,
}
OPEN_PATHS = {"/health"}
AUTH_EXEMPT_PATHS = {"/health", "/openapi.json"}
MAX_BODY_BYTES = 1_000_000
READ_LIMIT_PER_MINUTE = 120
WRITE_LIMIT_PER_MINUTE = 10
MAX_GROUP_LIMIT = 100_000

BackendParam = Annotated[
    Literal["restful", "native"] | None,
    Query(description="Force a specific upstream backend (disables fallback). Normally omitted."),
]
SessionIdParam = Annotated[str, Path(alias="sessionId", pattern=r"^[0-9a-fA-F-]{32,36}$")]
GroupParam = Annotated[
    str | None,
    Query(description="Native: single session group name (same as groupPath=<name>). Kept for compatibility."),
]
GroupPathParam = Annotated[
    list[str] | None,
    Query(
        alias="groupPath",
        description="Native: session group path (sessionGroupPathParts); repeat for nested groups. "
        "Default: the group ScreenConnect reports for the session.",
    ),
]
NativeSessionTypeParam = Annotated[
    int | None,
    Query(alias="sessionType", ge=0, le=2, description="Native lookup hint: 0 Support, 1 Meeting, 2 Access"),
]


def served(response: Response, result: Served[Any], *, listing: bool = False) -> Any:
    response.headers[BACKEND_HEADER] = result.backend
    if listing:
        response.headers[TRUNCATED_HEADER] = {True: "true", False: "false", None: "unknown"}[result.truncated]
        if result.total is not None:
            response.headers[TOTAL_HEADER] = str(result.total)
    if result.resolved_company is not None:
        response.headers[RESOLVED_COMPANY_HEADER] = quote(result.resolved_company, safe="")
    return result.value


def group_path_of(group: str | None, group_path: list[str] | None) -> list[str] | None:
    if group is not None and group_path is not None:
        raise BadRequestError("give either group or groupPath, not both")
    return group_path if group_path is not None else ([group] if group is not None else None)


def create_app(
    settings: Settings,
    *,
    restful_transport: httpx.AsyncBaseTransport | None = None,
    native_transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
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
        clock=clock,
    )
    gateway = Gateway(settings, restful, native, clock=clock, wall_clock=wall_clock)
    limiter = RateLimiter(
        {"read": READ_LIMIT_PER_MINUTE, "write": WRITE_LIMIT_PER_MINUTE}, clock=clock
    )
    # Writes without a valid key are audited up to 60/min; the overflow is counted and
    # reported on the next line written, so key-guessing volume stays visible.
    anonymous_audit_limiter = RateLimiter({"audit": 60}, clock=clock)
    suppressed_keyless = {"count": 0}
    health_cache: dict[str, tuple[float, dict[str, Any]]] = {}
    health_lock = asyncio.Lock()

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

    def error_response(exc: GatewayError) -> JSONResponse:
        headers = {BACKEND_HEADER: exc.backend or "none"}
        if isinstance(exc, RateLimitedError):
            headers["Retry-After"] = str(exc.retry_after)
        if exc.status_code >= 500:
            logger.warning("request failed: %s (%s)", exc.message, type(exc).__name__)
        body: dict[str, Any] = {"message": exc.message}
        if isinstance(exc, AmbiguousCompanyError):
            body["candidates"] = exc.candidates
        return JSONResponse(body, status_code=exc.status_code, headers=headers)

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
        return error_response(exc)

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

    # ---------- middleware: source IP, body size, auth, headers, audit ----------

    def precheck(request: Request) -> JSONResponse | None:
        """Runs before routing, so rejected requests never have their body parsed."""
        path = request.url.path
        host = request.client.host if request.client else None
        if path not in OPEN_PATHS and not source_ip_allowed(settings, host):
            return JSONResponse({"message": "source IP not allowed"}, status_code=403)
        if path not in AUTH_EXEMPT_PATHS:
            try:
                authorize(request)
            except GatewayError as exc:
                return error_response(exc)
        if request.method not in READ_METHODS:
            length = request.headers.get("content-length")
            if length is None and "transfer-encoding" in request.headers:
                return JSONResponse({"message": "Content-Length required"}, status_code=411)
            if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
                return JSONResponse({"message": "request body too large"}, status_code=413)
        return None

    @app.middleware("http")
    async def gateway_middleware(request: Request, call_next):
        is_write = request.method not in READ_METHODS
        response = precheck(request)
        if response is None:
            try:
                response = await call_next(request)
            except Exception:
                if is_write:
                    _audit(request, 500, None)
                raise
        response.headers.setdefault(BACKEND_HEADER, "none")
        response.headers["Cache-Control"] = "no-store"
        if is_write:
            _audit(request, response.status_code, response.headers.get(BACKEND_HEADER))
        return response

    def _audit(request: Request, status_code: int, backend: str | None) -> None:
        key_id = getattr(request.state, "key_id", None)
        suppressed = 0
        if key_id is None:
            if anonymous_audit_limiter.hit("anonymous", "audit") is not None:
                suppressed_keyless["count"] += 1
                return
            suppressed, suppressed_keyless["count"] = suppressed_keyless["count"], 0
        route = request.scope.get("route")
        audit_write(
            endpoint=f"{request.method} {getattr(route, 'path', request.url.path)}",
            session_id=request.scope.get("path_params", {}).get("sessionId"),
            key_id=key_id,
            backend=None if backend == "none" else backend,
            status_code=status_code,
            client_ip=request.client.host if request.client else None,
            suppressed_keyless=suppressed,
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
        # Unauthenticated endpoint: single-flight so a burst can't fan out to
        # ScreenConnect (or repeat failed logins and lock the service account).
        async with health_lock:
            cached = health_cache.get("health")
            if cached and cached[0] > clock():
                return cached[1]
            restful_health, native_health = await asyncio.gather(
                gateway.probe("restful"), gateway.probe("native")
            )
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

    v1 = APIRouter(prefix="/v1")

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
        responses=COMPANY_RESOLUTION_RESPONSES,
    )
    async def list_company_sessions(
        response: Response,
        company_id: Annotated[
            str,
            Path(
                alias="companyId",
                description="Company id as returned by /v1/companies. A lowercased/trimmed form "
                "(as stored by Rewst org mappings) also works when it matches exactly one company.",
            ),
        ],
        session_type: Annotated[int, Query(alias="sessionType", ge=0, le=2)] = 2,
        backend: BackendParam = None,
    ):
        return served(response, await gateway.list_company_sessions(company_id, session_type, backend), listing=True)

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
        responses=GROUP_RESOLUTION_RESPONSES,
    )
    async def list_group_sessions(
        response: Response,
        path: Annotated[list[str], Query(min_length=1, description="Group path segments, outermost first. Repeat the parameter.")],
        session_type: Annotated[int, Query(alias="sessionType", ge=0, le=2, description="0 Support, 1 Meeting, 2 Access")] = 2,
        filter: Annotated[
            str, Query(description="Free-text search within the group, like the ScreenConnect web UI search box")
        ] = "",
        limit: Annotated[int, Query(ge=1, le=MAX_GROUP_LIMIT, description="sessionLimit (GetLiveData has no paging)")] = 1000,
        find_session_id: Annotated[
            str, Query(alias="findSessionId", pattern=r"^([0-9a-fA-F-]{32,36})?$", description="findSessionID")
        ] = "",
        complete: Annotated[
            bool,
            Query(description="If the result is truncated, re-request with ScreenConnect's untruncated count "
                  f"(up to {MAX_GROUP_LIMIT:,}) so X-Gateway-Truncated is false"),
        ] = False,
        backend: BackendParam = None,
    ):
        result = await gateway.list_group_sessions(
            path, session_type, filter, limit, find_session_id=find_session_id, complete=complete, forced=backend
        )
        return served(response, result, listing=True)

    @v1.get(
        "/sessions",
        operation_id="search_sessions",
        response_model=list[models.Session],
        tags=["sessions"],
        responses=LIST_RESPONSES,
    )
    async def search_sessions(
        response: Response,
        filter: Annotated[
            str | None,
            Query(description="ScreenConnect session filter expression, e.g. GuestMachineName LIKE 'SRV*' (restful backend only)"),
        ] = None,
        name: Annotated[str | None, Query(description="Exact session name")] = None,
        session_type: Annotated[
            int | None,
            Query(alias="sessionType", ge=0, le=2, description="Omit for all session types (both backends)"),
        ] = None,
        backend: BackendParam = None,
    ):
        """Provide exactly one of `filter` or `name`. For the whole Access fleet, including sessions
        with a blank company, use filter=SessionType = 'Access'."""
        return served(
            response,
            await gateway.search_sessions(
                filter_expression=filter, name=name, session_type=session_type, forced=backend
            ),
            listing=True,
        )

    @v1.get("/sessions/{sessionId}", operation_id="get_session", response_model=models.Session, tags=["sessions"])
    async def get_session(
        response: Response,
        session_id: SessionIdParam,
        session_type: NativeSessionTypeParam = None,
        group_path: GroupPathParam = None,
        backend: BackendParam = None,
    ):
        result = await gateway.get_session(
            session_id, session_type=session_type, group_path=group_path, forced=backend
        )
        return served(response, result)

    @v1.get(
        "/sessions/{sessionId}/details",
        operation_id="get_session_details",
        response_model=models.SessionDetails,
        tags=["sessions"],
    )
    async def get_session_details(
        response: Response,
        session_id: SessionIdParam,
        group: GroupParam = None,
        group_path: GroupPathParam = None,
        session_type: NativeSessionTypeParam = None,
        backend: BackendParam = None,
    ):
        """Session, events and connections. Command output arrives as eventType 70 (RanCommand)."""
        result = await gateway.get_session_details(
            session_id, group_path=group_path_of(group, group_path), session_type=session_type, forced=backend
        )
        return served(response, result)

    @v1.get(
        "/sessions/{sessionId}/connection-logs",
        operation_id="get_connection_logs",
        response_model=models.ConnectionLogs,
        tags=["sessions"],
    )
    async def get_connection_logs(
        response: Response,
        session_id: SessionIdParam,
        group: GroupParam = None,
        group_path: GroupPathParam = None,
        session_type: NativeSessionTypeParam = None,
        backend: BackendParam = None,
    ):
        result = await gateway.get_connection_logs(
            session_id, group_path=group_path_of(group, group_path), session_type=session_type, forced=backend
        )
        return served(response, result)

    # ---------- raw compatibility reads: verbatim upstream payloads ----------

    @v1.get("/raw/restful/sessions", operation_id="raw_search_sessions", response_model=models.RawResponse, tags=["raw"])
    async def raw_search_sessions(
        response: Response,
        filter: Annotated[str | None, Query(description="GetSessionsByFilter sessionFilter")] = None,
        name: Annotated[str | None, Query(description="GetSessionsByName sessionName")] = None,
    ):
        """RESTful GetSessionsByFilter or GetSessionsByName (exactly one parameter)."""
        return served(response, await gateway.raw_restful_search(filter_expression=filter, name=name))

    @v1.get("/raw/restful/sessions/{sessionId}", operation_id="raw_get_session", response_model=models.RawResponse, tags=["raw"])
    async def raw_get_session(response: Response, session_id: SessionIdParam):
        """RESTful GetSessionBySessionID."""
        return served(response, await gateway.raw_restful_session(session_id))

    @v1.get(
        "/raw/restful/sessions/{sessionId}/details",
        operation_id="raw_get_session_details",
        response_model=models.RawResponse,
        tags=["raw"],
    )
    async def raw_get_session_details(response: Response, session_id: SessionIdParam):
        """RESTful GetSessionDetailsBySessionID (Session, Events, Connections, GuestScreenshotContent)."""
        return served(response, await gateway.raw_restful_details(session_id))

    @v1.get("/raw/native/live-data", operation_id="raw_get_live_data", response_model=models.RawResponse, tags=["raw"])
    async def raw_get_live_data(
        response: Response,
        session_type: Annotated[int, Query(alias="sessionType", ge=0, le=2)] = 2,
        path: Annotated[list[str] | None, Query(description="sessionGroupPathParts; omit for ScreenConnect's default")] = None,
        filter: Annotated[str, Query(description="Free-text search (web UI search box)")] = "",
        find_session_id: Annotated[
            str, Query(alias="findSessionId", pattern=r"^([0-9a-fA-F-]{32,36})?$")
        ] = "",
        limit: Annotated[int, Query(ge=1, le=MAX_GROUP_LIMIT, description="sessionLimit")] = 1000,
    ):
        """Native GetLiveData: Version, ProductVersion and the full ResponseInfoMap (HostSessionInfo with
        Sessions, PathSessionGroupSummaries, AlternateSessionGroupSummaries, SessionGroupPath, Filter,
        UntruncatedSessionCount; ActionCenterInfo). Native times are seconds before `receivedAt`."""
        result = await gateway.raw_live_data(
            session_type=session_type, path=path or [], filter_text=filter, find_session_id=find_session_id, limit=limit
        )
        return served(response, result)

    @v1.get(
        "/raw/native/sessions/{sessionId}/details",
        operation_id="raw_get_native_session_details",
        response_model=models.RawResponse,
        tags=["raw"],
    )
    async def raw_get_native_session_details(
        response: Response,
        session_id: SessionIdParam,
        group_path: GroupPathParam = None,
        session_type: NativeSessionTypeParam = None,
        items: Annotated[
            int,
            Query(description="SessionDetailsItems flags: 1 SessionInfo, 4 GuestScreenshotContent, 8 Events, "
                  "16 Connections, -1 All. Default 25 (everything except the screenshot)."),
        ] = DETAILS_SESSION_INFO | DETAILS_EVENTS | DETAILS_CONNECTIONS,
    ):
        """Native GetSessionDetails. Times are relative to the payload's BaseTime (epoch ms)."""
        result = await gateway.raw_native_details(
            session_id, group_path=group_path, session_type=session_type, items=items
        )
        return served(response, result)

    # ---------- writes ----------

    @v1.post(
        "/sessions",
        operation_id="create_session",
        response_model=models.CreatedSession,
        status_code=201,
        tags=["sessions"],
    )
    async def create_session(response: Response, body: models.CreateSessionRequest, backend: BackendParam = None):
        """RESTful CreateSession. All eight custom properties may be set at creation."""
        result = await gateway.create_session(
            session_type=body.session_type,
            name=body.name,
            is_public=body.is_public,
            code=body.code,
            changes=body.changes(),
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
        """Queue a command. Poll /details for eventType 70 (RanCommand) to read the output."""
        result = await gateway.send_command(
            session_id,
            command=body.command,
            shell=body.shell,
            timeout_ms=body.timeout_ms,
            group_path=group_path_of(body.group, body.group_path),
            session_type=body.session_type,
            forced=backend,
        )
        return served(response, result)

    @v1.post(
        "/session-events",
        operation_id="add_session_events",
        response_model=models.QueuedEvents,
        status_code=202,
        tags=["sessions"],
    )
    async def add_session_events(response: Response, body: models.SessionEventsRequest, backend: BackendParam = None):
        """Native AddSessionEvents: one batch of up to 100 events for sessions in one group path.
        Event types are validated against ScreenConnect's SessionEventType enum and ALLOWED_EVENT_TYPES."""
        events = [event.model_dump(by_alias=True) for event in body.events]
        result = await gateway.add_session_events(group_path=body.group_path, events=events, forced=backend)
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
        result = await gateway.update_custom_properties(
            session_id, body.changes(), group_path=body.group_path, session_type=body.session_type, forced=backend
        )
        return served(response, result)

    @v1.post(
        "/users",
        operation_id="create_user",
        response_model=models.CreatedUser,
        status_code=201,
        tags=["users"],
    )
    async def create_user(response: Response, body: models.CreateUserRequest, backend: BackendParam = None):
        result = await gateway.save_user(
            user_source=body.user_source,
            old_user_name=None,
            new_user_name=body.user_name,
            password=body.password.get_secret_value(),
            verify_password=None,
            password_question=body.password_question,
            display_name=body.display_name,
            comment=body.comment,
            email=body.email,
            roles=body.roles,
            force_password_change=body.force_password_change,
            forced=backend,
        )
        return served(response, replace(result, value={"userName": result.value["userName"], "created": True}))

    @v1.post(
        "/users/save",
        operation_id="save_user",
        response_model=models.SavedUser,
        tags=["users"],
    )
    async def save_user(response: Response, body: models.SaveUserRequest, backend: BackendParam = None):
        """Native SecurityService.SaveUser: create (oldUserName null), update, or rename (newUserName differs).
        Updates replace every field, so they must be stated explicitly; a null password keeps the current one."""
        result = await gateway.save_user(
            user_source=body.user_source,
            old_user_name=body.old_user_name,
            new_user_name=body.new_user_name,
            password=body.password.get_secret_value() if body.password is not None else None,
            verify_password=body.verify_password.get_secret_value() if body.verify_password is not None else None,
            password_question=body.password_question,
            display_name=body.display_name,
            comment=body.comment,
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
