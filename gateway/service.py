"""Gateway operations: one implementation per backend, plus backend selection/fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Generic, Literal, TypeVar

from .config import Settings
from .errors import AmbiguousCompanyError, BadRequestError, ForbiddenError, GatewayError, NotFoundError, UpstreamTimeoutError
from .normalize import (
    build_command,
    companies_from_sessions,
    companies_from_summaries,
    escape_filter_value,
    normalize_company_id,
    merge_custom_properties,
    normalize_connection,
    normalize_events,
    normalize_group_summary,
    normalize_session,
    normalize_session_group,
    pad_custom_properties,
)
from .upstream.native import NativeClient
from .upstream.restful import RestfulClient

logger = logging.getLogger("gateway.service")

Backend = Literal["restful", "native"]
T = TypeVar("T")
Operation = Callable[[], Awaitable[T]]

LIVE_DATA_SESSION_LIMIT = 10_000
COMMAND_EVENT_TYPE = 44
COMPANY_INDEX = 0


@dataclass(frozen=True)
class Served(Generic[T]):
    value: T
    backend: str
    resolved_company: str | None = None  # raw id, set when a non-exact company id was resolved


class Gateway:
    def __init__(
        self,
        settings: Settings,
        restful: RestfulClient,
        native: NativeClient,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.restful = restful
        self.native = native
        self._clients = {"restful": restful, "native": native}
        self._clock = clock
        self._company_cache: dict[str, tuple[float, Served[list[dict[str, Any]]]]] = {}
        self._company_lock = asyncio.Lock()

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

    async def _live_data(
        self,
        *,
        path: list[str],
        session_type: int,
        filter_expression: str = "",
        find_session_id: str = "",
        limit: int = LIVE_DATA_SESSION_LIMIT,
    ) -> dict[str, Any]:
        request = {
            "HostSessionInfo": {
                "sessionType": session_type,
                "sessionGroupPathParts": path,
                "filter": filter_expression,
                "findSessionID": find_session_id,
                "sessionLimit": limit,
            },
            "ActionCenterInfo": {},
        }
        data = await self.native.call("PageService.ashx/GetLiveData", [request, 0], idempotent=True)
        return ((data or {}).get("ResponseInfoMap") or {}).get("HostSessionInfo") or {}

    async def _native_find_session(self, session_id: str) -> dict[str, Any]:
        info = await self._live_data(
            path=[self.settings.native_default_group],
            session_type=2,
            find_session_id=session_id,
        )
        for raw in info.get("Sessions") or []:
            if str(raw.get("SessionID", "")).lower() == session_id.lower():
                return raw
        raise NotFoundError(f"session {session_id} not found")

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

        async def native() -> list[dict[str, Any]]:
            info = await self._live_data(
                path=[self.settings.company_group_path, company_id], session_type=session_type
            )
            return exact(info.get("Sessions") or [])

        served = await self._dispatch({"restful": restful, "native": native}, forced=forced)
        if not served.value:
            raise NotFoundError(f"no sessions found for company '{requested_id}'", backend=served.backend)
        return replace(served, resolved_company=company_id) if resolved else served

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
        forced: str | None = None,
    ) -> Served[dict[str, Any]]:
        resolved_company = None
        if len(path) >= 2 and path[0] == self.settings.company_group_path:
            raw_id, resolved = await self.resolve_company(path[1])
            if resolved:
                path = [path[0], raw_id, *path[2:]]
                resolved_company = raw_id

        async def native() -> dict[str, Any]:
            info = await self._live_data(
                path=path, session_type=session_type, filter_expression=filter_expression, limit=limit
            )
            levels = info.get("PathSessionGroupSummaries") or []
            last_level = levels[-1] if levels else []
            return {
                "groups": [normalize_group_summary(summary) for summary in last_level or []],
                "sessions": [normalize_session(raw) for raw in info.get("Sessions") or []],
            }

        served = await self._dispatch({"native": native}, forced=forced)
        return replace(served, resolved_company=resolved_company) if resolved_company else served

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

        async def native() -> list[dict[str, Any]]:
            info = await self._live_data(
                path=[self.settings.native_default_group],
                session_type=2 if session_type is None else session_type,
                filter_expression=name,  # free-text search, narrowed to exact names below
            )
            return [normalize_session(raw) for raw in info.get("Sessions") or [] if raw.get("Name") == name]

        return await self._dispatch({"restful": restful, "native": native}, forced=forced)

    async def get_session(self, session_id: str, forced: str | None = None) -> Served[dict[str, Any]]:
        async def restful() -> dict[str, Any]:
            return normalize_session(await self._restful_get_session(session_id))

        async def native() -> dict[str, Any]:
            return normalize_session(await self._native_find_session(session_id))

        return await self._dispatch({"restful": restful, "native": native}, forced=forced)

    async def get_session_details(
        self, session_id: str, group: str | None, forced: str | None = None
    ) -> Served[dict[str, Any]]:
        async def restful() -> dict[str, Any]:
            data = await self.restful.call("GetSessionDetailsBySessionID", [session_id], idempotent=True)
            if not data or not data.get("Session"):
                raise NotFoundError(f"session {session_id} not found")
            return {
                "session": normalize_session(data["Session"]),
                "events": normalize_events(data.get("Events")),
            }

        async def native() -> dict[str, Any]:
            data = await self._native_details(session_id, group)
            session = data["Session"]
            return {
                "session": normalize_session(session),
                "events": normalize_events(data.get("Events"), session.get("Events")),
            }

        return await self._dispatch({"restful": restful, "native": native}, forced=forced)

    async def get_connection_logs(
        self, session_id: str, group: str | None, forced: str | None = None
    ) -> Served[dict[str, Any]]:
        async def native() -> dict[str, Any]:
            data = await self._native_details(session_id, group)
            return {
                "sessionId": session_id,
                "connections": [normalize_connection(raw) for raw in data.get("Connections") or []],
            }

        return await self._dispatch({"native": native}, forced=forced)

    async def _native_details(self, session_id: str, group: str | None) -> dict[str, Any]:
        data = await self.native.call(
            "PageService.ashx/GetSessionDetails",
            [group or self.settings.native_default_group, session_id],
            idempotent=True,
        )
        if not data or not data.get("Session"):
            raise NotFoundError(f"session {session_id} not found")
        return data

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
            result = result or {}
            return {"sessionId": str(result.get("SessionID") or ""), "name": result.get("Name") or name}

        served = await self._dispatch({"restful": restful}, forced=forced, write=True)
        if changes.get(COMPANY_INDEX):
            self.invalidate_companies()
        return served

    async def send_command(
        self,
        session_id: str,
        *,
        command: str,
        shell: str,
        timeout_ms: int | None,
        group: str | None,
        forced: str | None = None,
    ) -> Served[dict[str, Any]]:
        script = build_command(command, shell, timeout_ms)

        async def restful() -> dict[str, Any]:
            await self.restful.call("SendCommandToSession", [session_id, script], idempotent=False)
            return {"sessionId": session_id, "queued": True}

        async def native() -> dict[str, Any]:
            event = {"SessionID": session_id, "EventType": COMMAND_EVENT_TYPE, "Data": script}
            await self.native.call(
                "PageService.ashx/AddSessionEvents",
                [[group or self.settings.native_default_group], [event]],
                idempotent=False,
            )
            return {"sessionId": session_id, "queued": True}

        return await self._dispatch({"restful": restful, "native": native}, forced=forced, write=True)

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
        self, session_id: str, changes: dict[int, str], forced: str | None = None
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
            current = pad_custom_properties((await self._native_find_session(session_id)).get("CustomPropertyValues"))
            merged = merge_custom_properties(current, changes)
            group_path = [self.settings.native_default_group]
            for index, value in enumerate(merged):
                if value != current[index]:
                    await self.native.call(
                        "PageService.ashx/UpdateSessionCustomPropertyValue",
                        [group_path, session_id, index, value],
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

    async def create_user(
        self,
        *,
        user_name: str,
        password: str,
        display_name: str,
        email: str,
        roles: list[str],
        force_password_change: bool,
        forced: str | None = None,
    ) -> Served[dict[str, Any]]:
        allowed = self.settings.allowed_user_roles
        if allowed and not set(roles) <= set(allowed):
            raise ForbiddenError(f"roles not allowed: {', '.join(sorted(set(roles) - set(allowed)))}")

        async def native() -> dict[str, Any]:
            args = [
                "InternalMembershipProvider",
                None,
                user_name,
                password,
                password,
                "",
                display_name,
                "",
                email,
                roles,
                force_password_change,
            ]
            await self.native.call("SecurityService.ashx/SaveUser", args, idempotent=False)
            return {"userName": user_name, "created": True}

        return await self._dispatch({"native": native}, forced=forced, write=True)

    async def aclose(self) -> None:
        await asyncio.gather(self.restful.aclose(), self.native.aclose())
