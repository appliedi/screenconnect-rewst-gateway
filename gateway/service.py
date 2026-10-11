"""Gateway operations: one implementation per backend, plus backend selection/fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Generic, Literal, TypeVar

from .config import Settings
from .errors import (
    AmbiguousCompanyError,
    BadRequestError,
    ForbiddenError,
    GatewayError,
    NotFoundError,
    UpstreamTimeoutError,
)
from .normalize import (
    absolutize_native_events,
    absolutize_native_live_session,
    build_command,
    companies_from_sessions,
    companies_from_summaries,
    escape_filter_value,
    merge_custom_properties,
    merge_native_details,
    normalize_company_id,
    normalize_connection,
    normalize_events,
    normalize_group_summary,
    normalize_session,
    normalize_session_group,
    omit_keys,
    pad_custom_properties,
)
from .sc_types import (
    RAW_OMITTED_KEYS,
    canonical_user_name,
    DETAILS_CONNECTIONS,
    DETAILS_EVENTS,
    DETAILS_SESSION_INFO,
    SESSION_EVENT_TYPES,
)
from .upstream.native import NativeClient
from .upstream.restful import RestfulClient

logger = logging.getLogger("gateway.service")

Backend = Literal["restful", "native"]
T = TypeVar("T")
Operation = Callable[[], Awaitable[T]]

LIVE_DATA_SESSION_LIMIT = 10_000
FIND_SESSION_LIMIT = 100
MAX_COMPLETE_SESSIONS = 100_000
LOOKUP_SESSION_TYPES = (2, 0, 1)  # Access first: by far the most common
GROUP_CACHE_SECONDS = 300
COMMAND_EVENT_TYPE = SESSION_EVENT_TYPES["QueuedCommand"]
COMPANY_INDEX = 0
DETAILS_FOR_DETAILS = DETAILS_SESSION_INFO | DETAILS_EVENTS | DETAILS_CONNECTIONS  # no screenshot


@dataclass(frozen=True)
class Served(Generic[T]):
    value: T
    backend: str
    resolved_company: str | None = None  # raw id, set when a non-exact company id was resolved
    truncated: bool | None = None  # list reads: None means the upstream gave no truncation signal
    total: int | None = None  # list reads: upstream's untruncated count, when it reports one


@dataclass(frozen=True)
class NativeLocation:
    session: dict[str, Any]
    path: list[str]
    session_type: int


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


class Gateway:
    def __init__(
        self,
        settings: Settings,
        restful: RestfulClient,
        native: NativeClient,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.settings = settings
        self._wall_clock = wall_clock
        self.restful = restful
        self.native = native
        self._clients = {"restful": restful, "native": native}
        self._clock = clock
        self._company_cache: dict[str, tuple[float, Served[list[dict[str, Any]]]]] = {}
        self._company_lock = asyncio.Lock()
        self._group_cache: tuple[float, list[dict[str, Any]]] | None = None

    # ---------- backend selection ----------

    def _preference(self) -> list[str]:
        if self.settings.backend_preference == "native-first":
            return ["native", "restful"]
        return ["restful", "native"]

    async def _dispatch(
        self,
        operations: dict[str, Operation[T]],
        *,
        forced: str | None = None,
        write: bool = False,
    ) -> Served[T]:
        # Per-call timeouts x retries x re-login x fallback could otherwise stack to minutes.
        try:
            async with asyncio.timeout(self.settings.request_deadline_seconds):
                return await self._dispatch_with_fallback(operations, forced=forced, write=write)
        except TimeoutError as exc:
            raise UpstreamTimeoutError("request deadline exceeded") from exc

    async def _dispatch_with_fallback(
        self,
        operations: dict[str, Operation[T]],
        *,
        forced: str | None,
        write: bool,
    ) -> Served[T]:
        if forced is not None:
            if forced not in operations:
                raise BadRequestError(f"backend '{forced}' is not supported by this endpoint")
            candidates = [forced]
        else:
            supported = [backend for backend in self._preference() if backend in operations]
            enabled = [backend for backend in supported if self._clients[backend].enabled]
            # With nothing enabled, call the first anyway so it raises a 503.
            candidates = enabled or supported[:1]

        for index, backend in enumerate(candidates):
            try:
                return Served(await operations[backend](), backend)
            except GatewayError as exc:
                exc.backend = exc.backend or backend
                is_last = index == len(candidates) - 1
                if is_last or not exc.fallback_ok or (write and not exc.write_safe):
                    raise
                logger.warning(
                    "%s backend failed (%s: %s); falling back to %s",
                    backend,
                    type(exc).__name__,
                    exc.message,
                    candidates[index + 1],
                )
        raise AssertionError("unreachable")

    # ---------- upstream helpers ----------

    async def _restful_sessions_by_filter(self, expression: str) -> list[dict[str, Any]]:
        return await self.restful.call("GetSessionsByFilter", [expression], idempotent=True) or []

    @staticmethod
    def _live_data_request(
        *, path: list[str], session_type: int, filter_expression: str, find_session_id: str, limit: int
    ) -> list[Any]:
        # Exactly the HostSessionInfo keys the ScreenConnect web client sends.
        host_session_info = {
            "sessionType": session_type,
            "sessionGroupPathParts": path,
            "filter": filter_expression,
            "findSessionID": find_session_id,
            "sessionLimit": limit,
        }
        return [{"HostSessionInfo": host_session_info, "ActionCenterInfo": {}}, 0]

    async def _live_data(
        self,
        *,
        path: list[str],
        session_type: int,
        filter_expression: str = "",
        find_session_id: str = "",
        limit: int = LIVE_DATA_SESSION_LIMIT,
    ) -> dict[str, Any]:
        request = self._live_data_request(
            path=path,
            session_type=session_type,
            filter_expression=filter_expression,
            find_session_id=find_session_id,
            limit=limit,
        )
        data = await self.native.call("PageService.ashx/GetLiveData", request, idempotent=True)
        received_at = self._wall_clock()
        info = dict(((data or {}).get("ResponseInfoMap") or {}).get("HostSessionInfo") or {})
        info["Sessions"] = [absolutize_native_live_session(raw, received_at) for raw in info.get("Sessions") or []]
        return info

    async def _live_data_complete(self, **kwargs: Any) -> dict[str, Any]:
        """GetLiveData has no paging, only sessionLimit: when truncated, re-ask for the full count."""
        info = await self._live_data(**kwargs)
        total = info.get("UntruncatedSessionCount")
        if self._truncated(info) and isinstance(total, int) and total <= MAX_COMPLETE_SESSIONS:
            info = await self._live_data(**{**kwargs, "limit": total})
        return info

    @staticmethod
    def _truncated(info: dict[str, Any]) -> bool | None:
        total = info.get("UntruncatedSessionCount")
        if not isinstance(total, int):
            return None
        return total > len(info.get("Sessions") or [])

    @staticmethod
    def _total(info: dict[str, Any]) -> int | None:
        total = info.get("UntruncatedSessionCount")
        return total if isinstance(total, int) else None

    async def _session_groups(self) -> list[dict[str, Any]]:
        if self._group_cache and self._group_cache[0] > self._clock():
            return self._group_cache[1]
        groups = await self.native.call("SessionGroupService.ashx/GetSessionGroups", [], idempotent=True) or []
        self._group_cache = (self._clock() + GROUP_CACHE_SECONDS, groups)
        return groups

    async def _all_sessions_path(self, session_type: int) -> list[str] | None:
        """The unfiltered, non-subgrouped group for a session type (e.g. "All Sessions",
        "All Meetings", "All Machines"), or None if the instance has none."""
        names = [
            group.get("Name")
            for group in await self._session_groups()
            if group.get("SessionType") == session_type
            and not group.get("SessionFilter")
            and not group.get("SubgroupExpressions")
            and group.get("Name")
        ]
        if session_type == 2 and self.settings.native_default_group in names:
            return [self.settings.native_default_group]
        return [names[0]] if names else None

    async def _native_find_session(
        self,
        session_id: str,
        *,
        session_type: int | None = None,
        group_path: list[str] | None = None,
    ) -> NativeLocation:
        """Locate a session the way the web client does: findSessionID with the session's type.
        Without a type, try Access, Support, Meeting in turn (at most three reads)."""
        types = [session_type] if session_type is not None else list(LOOKUP_SESSION_TYPES)
        for current_type in types:
            info = await self._live_data(
                path=list(group_path or []),
                session_type=current_type,
                find_session_id=session_id,
                limit=FIND_SESSION_LIMIT,
            )
            for raw in info.get("Sessions") or []:
                if str(raw.get("SessionID", "")).lower() == session_id.lower():
                    path = list(group_path or info.get("SessionGroupPath") or [])
                    if not path:
                        path = await self._all_sessions_path(current_type) or []
                    return NativeLocation(raw, path, current_type)
        raise NotFoundError(f"session {session_id} not found")

    async def _native_details(
        self, session_id: str, path_or_name: list[str] | str, items: int
    ) -> dict[str, Any]:
        data = await self.native.call(
            "PageService.ashx/GetSessionDetails", [path_or_name, session_id, items], idempotent=True
        )
        if not data or (items & DETAILS_SESSION_INFO and not data.get("Session")):
            raise NotFoundError(f"session {session_id} not found")
        return data

    async def _restful_get_session(self, session_id: str) -> dict[str, Any]:
        result = await self.restful.call("GetSessionBySessionID", [session_id], idempotent=True)
        sessions = result if isinstance(result, list) else [result] if result else []
        if not sessions:
            raise NotFoundError(f"session {session_id} not found")
        return sessions[0]

    # ---------- health ----------

    async def probe(self, backend: str) -> dict[str, Any]:
        client = self._clients[backend]
        if not client.enabled:
            return {"ok": False, "enabled": False}
        try:
            async with asyncio.timeout(self.settings.request_deadline_seconds):
                if backend == "restful":
                    await self.restful.call("GetSessionsByName", ["__gateway_health_probe__"], idempotent=True)
                else:
                    await self.native.call("SessionGroupService.ashx/GetSessionGroups", [], idempotent=True)
        except (GatewayError, TimeoutError) as exc:
            # Details stay in the log: /health is unauthenticated.
            logger.warning("%s health probe failed: %s", backend, getattr(exc, "message", "deadline exceeded"))
            return {"ok": False, "enabled": True}
        return {"ok": True, "enabled": True}

    # ---------- companies ----------

    async def list_companies(self, forced: str | None = None) -> Served[list[dict[str, Any]]]:
        cache_key = forced or "auto"
        cached = self._cached_companies(cache_key)
        if cached is not None:
            return cached
        async with self._company_lock:
            cached = self._cached_companies(cache_key)
            if cached is not None:
                return cached
            served = await self._dispatch(
                {"native": self._companies_native, "restful": self._companies_restful},
                forced=forced,
            )
            if self.settings.company_cache_seconds > 0:
                expires = self._clock() + self.settings.company_cache_seconds
                self._company_cache[cache_key] = (expires, served)
            return served

    def _cached_companies(self, cache_key: str) -> Served[list[dict[str, Any]]] | None:
        entry = self._company_cache.get(cache_key)
        if entry and entry[0] > self._clock():
            return entry[1]
        return None

    def invalidate_companies(self) -> None:
        self._company_cache.clear()

    async def _companies_native(self) -> list[dict[str, Any]]:
        info = await self._live_data(path=[self.settings.company_group_path], session_type=2, limit=1)
        return companies_from_summaries(info.get("PathSessionGroupSummaries"))

    async def _companies_restful(self) -> list[dict[str, Any]]:
        return companies_from_sessions(await self._restful_sessions_by_filter("CustomProperty1 <> ''"))

    async def resolve_company(self, company_id: str) -> tuple[str, bool]:
        """Map a company id to the raw stored value: exact match first, then a unique
        case/whitespace-insensitive match. Returns (raw id, whether it was resolved)."""
        try:
            companies = (await self.list_companies()).value
        except GatewayError:
            return company_id, False  # can't resolve; let the exact lookup decide
        raw_ids = [company["id"] for company in companies]
        if company_id in raw_ids:
            return company_id, False
        wanted = normalize_company_id(company_id)
        matches = sorted(raw_id for raw_id in raw_ids if normalize_company_id(raw_id) == wanted)
        if len(matches) > 1:
            raise AmbiguousCompanyError("ambiguous company id", candidates=matches)
        if matches:
            return matches[0], True
        return company_id, False

    async def list_company_sessions(
        self, company_id: str, session_type: int, forced: str | None = None
    ) -> Served[list[dict[str, Any]]]:
        requested_id = company_id
        company_id, resolved = await self.resolve_company(company_id)

        def exact(raw_sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
            # Exact match on the raw value: " Acme" and "Acme" are different companies.
            return [
                normalize_session(raw)
                for raw in raw_sessions
                if pad_custom_properties(raw.get("CustomPropertyValues"))[COMPANY_INDEX] == company_id
                and raw.get("SessionType", session_type) == session_type
            ]

        async def restful() -> list[dict[str, Any]]:
            expression = f"CustomProperty1 = '{escape_filter_value(company_id)}'"
            return exact(await self._restful_sessions_by_filter(expression))

        native_info: dict[str, Any] = {}

        async def native() -> list[dict[str, Any]]:
            native_info.update(
                await self._live_data_complete(
                    path=[self.settings.company_group_path, company_id], session_type=session_type
                )
            )
            return exact(native_info.get("Sessions") or [])

        served = await self._dispatch({"restful": restful, "native": native}, forced=forced)
        if not served.value:
            raise NotFoundError(f"no sessions found for company '{requested_id}'", backend=served.backend)
        is_native = served.backend == "native"
        return replace(
            served,
            resolved_company=company_id if resolved else None,
            truncated=self._truncated(native_info) if is_native else None,
        )

    # ---------- session groups ----------

    async def list_session_groups(self, forced: str | None = None) -> Served[list[dict[str, Any]]]:
        async def native() -> list[dict[str, Any]]:
            groups = await self.native.call("SessionGroupService.ashx/GetSessionGroups", [], idempotent=True)
            return [normalize_session_group(group) for group in groups or []]

        return await self._dispatch({"native": native}, forced=forced)

    async def list_group_sessions(
        self,
        path: list[str],
        session_type: int,
        filter_expression: str,
        limit: int,
        *,
        find_session_id: str = "",
        complete: bool = False,
        forced: str | None = None,
    ) -> Served[dict[str, Any]]:
        resolved_company = None
        if len(path) >= 2 and path[0] == self.settings.company_group_path:
            raw_id, resolved = await self.resolve_company(path[1])
            if resolved:
                path = [path[0], raw_id, *path[2:]]
                resolved_company = raw_id

        native_info: dict[str, Any] = {}

        async def native() -> dict[str, Any]:
            arguments = {
                "path": path,
                "session_type": session_type,
                "filter_expression": filter_expression,
                "find_session_id": find_session_id,
                "limit": limit,
            }
            info = await (self._live_data_complete(**arguments) if complete else self._live_data(**arguments))
            native_info.update(info)
            levels = info.get("PathSessionGroupSummaries") or []
            last_level = levels[-1] if levels else []
            return {
                "groups": [normalize_group_summary(summary) for summary in last_level or []],
                "sessions": [normalize_session(raw) for raw in info.get("Sessions") or []],
            }

        served = await self._dispatch({"native": native}, forced=forced)
        return replace(
            served,
            resolved_company=resolved_company,
            truncated=self._truncated(native_info),
            total=self._total(native_info),
        )

    # ---------- sessions (reads) ----------

    async def search_sessions(
        self,
        *,
        filter_expression: str | None,
        name: str | None,
        session_type: int | None,
        forced: str | None = None,
    ) -> Served[list[dict[str, Any]]]:
        if (filter_expression is None) == (name is None):
            raise BadRequestError("provide exactly one of 'filter' or 'name'")

        def of_type(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if session_type is None:
                return sessions
            return [session for session in sessions if session["sessionType"] == session_type]

        async def restful() -> list[dict[str, Any]]:
            if name is not None:
                raw = await self.restful.call("GetSessionsByName", [name], idempotent=True) or []
            else:
                raw = await self._restful_sessions_by_filter(filter_expression or "")
            return of_type([normalize_session(item) for item in raw])

        if name is None:
            # The native API's "filter" is the web UI's free-text search box, not an
            # expression language, so expressions are restful-only (no silent empty fallback).
            if forced == "native":
                raise BadRequestError("filter expressions require the restful backend")
            return await self._dispatch({"restful": restful}, forced=forced)

        native_truncated: list[bool | None] = []

        async def native() -> list[dict[str, Any]]:
            # Restful GetSessionsByName covers every session type, so without a sessionType the
            # native search covers each type's unfiltered group too, rather than Access only.
            types = [session_type] if session_type is not None else list(LOOKUP_SESSION_TYPES)
            found: list[dict[str, Any]] = []
            for current_type in types:
                path = await self._all_sessions_path(current_type)
                if path is None:
                    native_truncated.append(None)  # this type couldn't be searched
                    continue
                info = await self._live_data_complete(
                    path=path,
                    session_type=current_type,
                    filter_expression=name,  # free-text search, narrowed to exact names below
                )
                native_truncated.append(self._truncated(info))
                found.extend(normalize_session(raw) for raw in info.get("Sessions") or [] if raw.get("Name") == name)
            return found

        served = await self._dispatch({"restful": restful, "native": native}, forced=forced)
        if served.backend != "native":
            return served
        truncated = None if None in native_truncated else any(native_truncated)
        return replace(served, truncated=truncated)

    async def get_session(
        self,
        session_id: str,
        *,
        session_type: int | None = None,
        group_path: list[str] | None = None,
        forced: str | None = None,
    ) -> Served[dict[str, Any]]:
        async def restful() -> dict[str, Any]:
            return normalize_session(await self._restful_get_session(session_id))

        async def native() -> dict[str, Any]:
            location = await self._native_find_session(session_id, session_type=session_type, group_path=group_path)
            session = location.session
            try:
                details = await self._native_details(session_id, location.path, DETAILS_SESSION_INFO)
                session = merge_native_details(session, details)
            except GatewayError as exc:
                # One extra read for a single session; without it the detail-only fields stay null.
                logger.info("native details unavailable for %s: %s", session_id, exc.message)
            return normalize_session(session)

        return await self._dispatch({"restful": restful, "native": native}, forced=forced)

    async def get_session_details(
        self,
        session_id: str,
        *,
        group_path: list[str] | None = None,
        session_type: int | None = None,
        forced: str | None = None,
    ) -> Served[dict[str, Any]]:
        async def restful() -> dict[str, Any]:
            data = await self.restful.call("GetSessionDetailsBySessionID", [session_id], idempotent=True)
            if not data or not data.get("Session"):
                raise NotFoundError(f"session {session_id} not found")
            return {
                "session": normalize_session(data["Session"]),
                "events": normalize_events(data.get("Events")),
                "connections": [normalize_connection(raw) for raw in data.get("Connections") or []],
            }

        async def native() -> dict[str, Any]:
            # The details Session object has no SessionID/Name/custom properties; take those
            # from the live-data record and merge the detail-only fields on top.
            location = await self._native_find_session(session_id, session_type=session_type, group_path=group_path)
            data = await self._native_details(session_id, location.path, DETAILS_FOR_DETAILS)
            return {
                "session": normalize_session(merge_native_details(location.session, data)),
                "events": normalize_events(absolutize_native_events(data)),
                "connections": [normalize_connection(raw) for raw in data.get("Connections") or []],
            }

        return await self._dispatch({"restful": restful, "native": native}, forced=forced)

    async def get_connection_logs(
        self,
        session_id: str,
        *,
        group_path: list[str] | None = None,
        session_type: int | None = None,
        forced: str | None = None,
    ) -> Served[dict[str, Any]]:
        async def restful() -> dict[str, Any]:
            data = await self.restful.call("GetSessionDetailsBySessionID", [session_id], idempotent=True)
            if not data or not data.get("Session"):
                raise NotFoundError(f"session {session_id} not found")
            return {
                "sessionId": session_id,
                "connections": [normalize_connection(raw) for raw in data.get("Connections") or []],
            }

        async def native() -> dict[str, Any]:
            path = group_path
            if path is None:
                path = (
                    await self._native_find_session(session_id, session_type=session_type)
                ).path
            data = await self._native_details(session_id, path, DETAILS_CONNECTIONS)
            return {
                "sessionId": session_id,
                "connections": [normalize_connection(raw) for raw in data.get("Connections") or []],
            }

        return await self._dispatch({"restful": restful, "native": native}, forced=forced)

    # ---------- raw compatibility reads (verbatim upstream payloads) ----------

    async def _raw(self, backend: str, method: str, args: list[Any]) -> Served[dict[str, Any]]:
        client = self._clients[backend]

        async def operation() -> dict[str, Any]:
            response = await client.call(method, args, idempotent=True)
            return {
                "backend": backend,
                "upstreamMethod": method,
                "receivedAt": _iso(self._wall_clock()),
                "omittedFields": list(RAW_OMITTED_KEYS),
                "response": omit_keys(response, RAW_OMITTED_KEYS),
            }

        return await self._dispatch({backend: operation})

    async def raw_restful_search(self, *, filter_expression: str | None, name: str | None) -> Served[dict[str, Any]]:
        if (filter_expression is None) == (name is None):
            raise BadRequestError("provide exactly one of 'filter' or 'name'")
        if name is not None:
            return await self._raw("restful", "GetSessionsByName", [name])
        return await self._raw("restful", "GetSessionsByFilter", [filter_expression])

    async def raw_restful_session(self, session_id: str) -> Served[dict[str, Any]]:
        return await self._raw("restful", "GetSessionBySessionID", [session_id])

    async def raw_restful_details(self, session_id: str) -> Served[dict[str, Any]]:
        return await self._raw("restful", "GetSessionDetailsBySessionID", [session_id])

    async def raw_live_data(
        self, *, session_type: int, path: list[str], filter_text: str, find_session_id: str, limit: int
    ) -> Served[dict[str, Any]]:
        request = self._live_data_request(
            path=path,
            session_type=session_type,
            filter_expression=filter_text,
            find_session_id=find_session_id,
            limit=limit,
        )
        return await self._raw("native", "PageService.ashx/GetLiveData", request)

    async def raw_native_details(
        self, session_id: str, *, group_path: list[str] | None, session_type: int | None, items: int
    ) -> Served[dict[str, Any]]:
        path = group_path
        if path is None:
            path = (await self._native_find_session(session_id, session_type=session_type)).path
        return await self._raw("native", "PageService.ashx/GetSessionDetails", [path, session_id, items])

    # ---------- writes ----------

    async def create_session(
        self,
        *,
        session_type: int,
        name: str,
        is_public: bool,
        code: str | None,
        changes: dict[int, str],
        forced: str | None = None,
    ) -> Served[dict[str, Any]]:
        async def restful() -> dict[str, Any]:
            properties = merge_custom_properties([], changes)
            result = await self.restful.call(
                "CreateSession", [session_type, name, is_public, code, properties], idempotent=False
            )
            raw = result if isinstance(result, dict) else None
            return {
                "sessionId": str((raw or {}).get("SessionID") or ""),
                "name": (raw or {}).get("Name") or name,
                "raw": raw,
            }

        served = await self._dispatch({"restful": restful}, forced=forced, write=True)
        if changes.get(COMPANY_INDEX):
            self.invalidate_companies()
        return served

    async def _native_write_path(
        self, session_id: str, group_path: list[str] | None, session_type: int | None
    ) -> list[str]:
        if group_path is not None:
            return group_path
        return (await self._native_find_session(session_id, session_type=session_type)).path

    async def send_command(
        self,
        session_id: str,
        *,
        command: str,
        shell: str,
        timeout_ms: int | None,
        group_path: list[str] | None = None,
        session_type: int | None = None,
        forced: str | None = None,
    ) -> Served[dict[str, Any]]:
        script = build_command(command, shell, timeout_ms)

        async def restful() -> dict[str, Any]:
            await self.restful.call("SendCommandToSession", [session_id, script], idempotent=False)
            return {"sessionId": session_id, "queued": True}

        async def native() -> dict[str, Any]:
            path = await self._native_write_path(session_id, group_path, session_type)
            event = {"SessionID": session_id, "EventType": COMMAND_EVENT_TYPE, "Data": script}
            await self.native.call("PageService.ashx/AddSessionEvents", [path, [event]], idempotent=False)
            return {"sessionId": session_id, "queued": True}

        return await self._dispatch({"restful": restful, "native": native}, forced=forced, write=True)

    async def add_session_events(
        self, *, group_path: list[str], events: list[dict[str, Any]], forced: str | None = None
    ) -> Served[dict[str, Any]]:
        # Fail closed: the configured (or conservative default) allowlist always applies.
        disallowed = sorted({event["eventType"] for event in events} - self.settings.allowed_event_types)
        if disallowed:
            raise ForbiddenError(f"event types not allowed: {', '.join(map(str, disallowed))}")
        entries = []
        for event in events:
            entry = {"SessionID": event["sessionId"], "EventType": event["eventType"]}
            for source, target in (("data", "Data"), ("connectionId", "ConnectionID"), ("correlationEventId", "CorrelationEventID")):
                if event.get(source) is not None:
                    entry[target] = event[source]
            entries.append(entry)

        async def native() -> dict[str, Any]:
            await self.native.call("PageService.ashx/AddSessionEvents", [group_path, entries], idempotent=False)
            session_ids = list(dict.fromkeys(entry["SessionID"] for entry in entries))
            return {"queued": len(entries), "sessionIds": session_ids}

        return await self._dispatch({"native": native}, forced=forced, write=True)

    async def _restful_only_write(
        self, method: str, args: list[Any], result: dict[str, Any], forced: str | None
    ) -> Served[dict[str, Any]]:
        async def restful() -> dict[str, Any]:
            await self.restful.call(method, args, idempotent=False)
            return result

        return await self._dispatch({"restful": restful}, forced=forced, write=True)

    async def send_message(self, session_id: str, sender: str, message: str, forced: str | None = None):
        return await self._restful_only_write(
            "SendMessageToSession", [session_id, sender, message], {"sessionId": session_id, "queued": True}, forced
        )

    async def add_note(self, session_id: str, note: str, forced: str | None = None):
        return await self._restful_only_write(
            "AddNoteToSession", [session_id, note], {"sessionId": session_id, "ok": True}, forced
        )

    async def run_toolbox_item(self, session_id: str, item_name: str, forced: str | None = None):
        return await self._restful_only_write(
            "SendToolboxItemToSession", [session_id, item_name], {"sessionId": session_id, "queued": True}, forced
        )

    async def rename_session(self, session_id: str, name: str, forced: str | None = None):
        return await self._restful_only_write(
            "UpdateSessionName", [session_id, name], {"sessionId": session_id, "name": name}, forced
        )

    async def update_custom_properties(
        self,
        session_id: str,
        changes: dict[int, str],
        *,
        group_path: list[str] | None = None,
        session_type: int | None = None,
        forced: str | None = None,
    ) -> Served[dict[str, Any]]:
        if not changes:
            raise BadRequestError("no custom property changes supplied")
        company_changed = False

        async def restful() -> dict[str, Any]:
            nonlocal company_changed
            current = pad_custom_properties((await self._restful_get_session(session_id)).get("CustomPropertyValues"))
            merged = merge_custom_properties(current, changes)
            await self.restful.call("UpdateSessionCustomProperties", [session_id, merged], idempotent=False)
            company_changed = merged[COMPANY_INDEX] != current[COMPANY_INDEX]
            return {"sessionId": session_id, "customProperties": merged}

        async def native() -> dict[str, Any]:
            nonlocal company_changed
            location = await self._native_find_session(session_id, session_type=session_type, group_path=group_path)
            current = pad_custom_properties(location.session.get("CustomPropertyValues"))
            merged = merge_custom_properties(current, changes)
            # Caller-supplied sessionGroupPathParts are passed through unchanged.
            path = group_path if group_path is not None else location.path
            for index, value in enumerate(merged):
                if value != current[index]:
                    await self.native.call(
                        "PageService.ashx/UpdateSessionCustomPropertyValue",
                        [path, session_id, index, value],
                        idempotent=False,
                    )
                    if index == COMPANY_INDEX:
                        company_changed = True
            return {"sessionId": session_id, "customProperties": merged}

        try:
            return await self._dispatch({"restful": restful, "native": native}, forced=forced, write=True)
        finally:
            if company_changed:
                self.invalidate_companies()

    async def save_user(
        self,
        *,
        user_source: str,
        old_user_name: str | None,
        new_user_name: str,
        password: str | None,
        verify_password: str | None,
        password_question: str | None,
        display_name: str,
        comment: str,
        email: str,
        roles: list[str],
        force_password_change: bool,
        forced: str | None = None,
    ) -> Served[dict[str, Any]]:
        # Fail closed: user management needs an explicit role allowlist ("*" = any role).
        allowed = self.settings.allowed_user_roles
        if not allowed:
            raise ForbiddenError("user management is disabled; set ALLOWED_USER_ROLES to enable it")
        if "*" not in allowed and not set(roles) <= set(allowed):
            raise ForbiddenError(f"roles not allowed: {', '.join(sorted(set(roles) - set(allowed)))}")
        # Compare in a canonical form so case, Unicode look-alikes or padding can't slip past.
        service_account = canonical_user_name(self.settings.sc_username) if self.settings.sc_username else ""
        if service_account and service_account in {
            canonical_user_name(name) for name in (old_user_name, new_user_name) if name
        }:
            raise ForbiddenError("the gateway's own service account cannot be created, changed or renamed")

        async def native() -> dict[str, Any]:
            # SecurityService.SaveUser argument order, as sent by the ScreenConnect admin UI.
            args = [
                user_source,
                old_user_name,
                new_user_name,
                password,
                verify_password if verify_password is not None else password,
                password_question,
                display_name,
                comment,
                email,
                roles,
                force_password_change,
            ]
            await self.native.call("SecurityService.ashx/SaveUser", args, idempotent=False)
            return {"userName": new_user_name, "oldUserName": old_user_name, "created": old_user_name is None}

        return await self._dispatch({"native": native}, forced=forced, write=True)

    async def aclose(self) -> None:
        await asyncio.gather(self.restful.aclose(), self.native.aclose())
