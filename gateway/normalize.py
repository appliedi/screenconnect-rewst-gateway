"""Pure functions that shape upstream payloads into the gateway's API."""

from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

CUSTOM_PROPERTY_COUNT = 8
AGENT_PROCESS_TYPE = 2
ACCESS_SESSION_TYPE = 2
# LogonSessionAttributes bits, inferred from live data (not documented by ConnectWise):
LOGON_INTERACTIVE_FLAG = 0x1  # a Windows logon session (absent on "[Backstage]")
LOGON_NO_USER_FLAG = 0x8  # nobody signed in at that session (DisplayName "Console")
# ScreenConnect's "never" is 0001-01-01 (restful) or -1.797e308 seconds (native).
MIN_VALID_YEAR = 2000
MAX_NATIVE_AGE_SECONDS = 50 * 365 * 86400


def pad_custom_properties(values: Any) -> list[str]:
    items = list(values or [])[:CUSTOM_PROPERTY_COUNT]
    strings = ["" if value is None else str(value) for value in items]
    return strings + [""] * (CUSTOM_PROPERTY_COUNT - len(strings))


def normalize_company_id(value: str) -> str:
    """Rewst stores mapping ids lowercased and trimmed; compare on that form."""
    return " ".join(value.split()).casefold()


def escape_filter_value(value: str) -> str:
    """ScreenConnect filter strings are single-quoted; quotes are escaped by doubling."""
    return value.replace("'", "''")


def _first(*values: Any) -> Any:
    """First value that isn't null/blank; strings are trimmed (a serial of " " means none)."""
    for value in values:
        if isinstance(value, str):
            if value.strip():
                return value.strip()
        elif value is not None:
            return value
    return None


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def normalize_timestamp(value: Any) -> str | None:
    """ISO-8601 UTC ("...Z"), or None for missing, unparseable, timezone-less or sentinel values."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.year < MIN_VALID_YEAR:
        return None
    return _iso_utc(parsed)


def seconds_before(seconds: Any, reference: datetime) -> str | None:
    """The native API reports times as seconds before a reference instant."""
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        return None
    if not math.isfinite(seconds) or not 0 <= seconds <= MAX_NATIVE_AGE_SECONDS:
        return None
    return _iso_utc(reference - timedelta(seconds=seconds))


def absolutize_native_live_session(raw: dict[str, Any], received_at: datetime) -> dict[str, Any]:
    """GetLiveData's LastGuestDisconnectedEventTime is seconds before the response; verified
    against restful within ~2 s. Its LastConnectedEventTime also counts host connections, so it
    is deliberately NOT used as lastGuestConnectedEventTime."""
    converted = dict(raw)
    converted["LastGuestDisconnectedEventTime"] = seconds_before(
        raw.get("LastGuestDisconnectedEventTime"), received_at
    )
    return converted


NATIVE_DETAIL_FIELDS = ("GuestMachineSerialNumber", "GuestHardwareNetworkAddress")


def merge_native_details(live: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    """GetSessionDetails' Session lacks SessionID/Name/custom properties but carries serial,
    MAC (both verified equal to restful) and GuestInfoUpdateTime as seconds before BaseTime (ms)."""
    session = details.get("Session") or {}
    merged = dict(live)
    for key in NATIVE_DETAIL_FIELDS:
        if key in session:
            merged[key] = session[key]
    reference = _native_base_time(details)
    merged["GuestInfoUpdateTime"] = seconds_before(session.get("GuestInfoUpdateTime"), reference) if reference else None
    return merged


def _native_base_time(details: dict[str, Any]) -> datetime | None:
    base = details.get("BaseTime")  # epoch milliseconds
    if isinstance(base, int | float) and not isinstance(base, bool) and math.isfinite(base):
        return datetime.fromtimestamp(base / 1000, UTC)
    return None


def absolutize_native_events(details: dict[str, Any]) -> list[dict[str, Any]]:
    """GetSessionDetails event Time is milliseconds before BaseTime (verified to 1 ms vs restful)."""
    reference = _native_base_time(details)
    events = []
    for event in details.get("Events") or []:
        time = event.get("Time")
        converted = seconds_before(time / 1000, reference) if reference and isinstance(time, int | float) else None
        events.append({**event, "Time": converted})
    return events


def logged_on_users(raw: dict[str, Any]) -> list[str]:
    """Signed-in users from LogonSessions. Current agents report users here rather
    than in the legacy GuestLoggedOnUserName / GuestInfo.LoggedOnUserName fields."""
    users: list[str] = []
    for logon in raw.get("LogonSessions") or []:
        attributes = logon.get("LogonSessionAttributes") or 0
        name = logon.get("DisplayName") or ""
        if (
            attributes & LOGON_INTERACTIVE_FLAG
            and not attributes & LOGON_NO_USER_FLAG
            and name
            and not name.startswith("[")
            and name not in users
        ):
            users.append(name)
    return users


def normalize_session(raw: dict[str, Any]) -> dict[str, Any]:
    """Handles both upstream shapes: native has flat Guest* fields, restful nests them in GuestInfo."""
    properties = pad_custom_properties(raw.get("CustomPropertyValues"))
    guest = raw.get("GuestInfo") or {}
    users = logged_on_users(raw)
    is_online = None
    if "ActiveConnections" in raw:
        is_online = any(
            connection.get("ProcessType") == AGENT_PROCESS_TYPE
            for connection in raw.get("ActiveConnections") or []
        )
    return {
        "sessionId": str(raw.get("SessionID") or ""),
        "name": raw.get("Name"),
        "sessionType": raw.get("SessionType"),
        "guestMachineName": _first(raw.get("GuestMachineName"), guest.get("MachineName")),
        "guestLoggedOnUserName": _first(*users[:1], raw.get("GuestLoggedOnUserName"), guest.get("LoggedOnUserName")),
        "loggedOnUsers": users,
        "operatingSystem": _first(raw.get("GuestOperatingSystemName"), guest.get("OperatingSystemName")),
        "company": properties[0],
        "site": properties[1],
        "department": properties[2],
        "deviceType": properties[3],
        "customProperties": properties,
        "isOnline": is_online,
        "guestMachineSerialNumber": _first(raw.get("GuestMachineSerialNumber"), guest.get("MachineSerialNumber")),
        "guestHardwareNetworkAddress": _first(
            raw.get("GuestHardwareNetworkAddress"), guest.get("HardwareNetworkAddress")
        ),
        "guestClientVersion": _first(raw.get("GuestClientVersion")),
        "guestInfoUpdateTime": normalize_timestamp(raw.get("GuestInfoUpdateTime")),
        "lastGuestConnectedEventTime": normalize_timestamp(raw.get("LastGuestConnectedEventTime")),
        "lastGuestDisconnectedEventTime": normalize_timestamp(raw.get("LastGuestDisconnectedEventTime")),
        "isDeleted": _flag(raw.get("IsDeleted")),
        "isEnded": _flag(raw.get("IsEnded")),
    }


def normalize_event(raw: dict[str, Any]) -> dict[str, Any]:
    event_id = raw.get("EventID")
    return {
        "eventId": None if event_id is None else str(event_id),
        "eventType": raw.get("EventType"),
        "time": raw.get("Time") if isinstance(raw.get("Time"), str) else None,
        "data": raw.get("Data"),
    }


def normalize_events(*event_lists: Any) -> list[dict[str, Any]]:
    seen: set[str] = set()
    events = []
    for event_list in event_lists:
        for raw in event_list or []:
            event = normalize_event(raw)
            if event["eventId"] is not None:
                if event["eventId"] in seen:
                    continue
                seen.add(event["eventId"])
            events.append(event)
    return events


def normalize_connection(raw: dict[str, Any]) -> dict[str, Any]:
    connection_id = raw.get("ConnectionID")
    return {
        "connectionId": None if connection_id is None else str(connection_id),
        "processType": raw.get("ProcessType"),
        "participantName": raw.get("ParticipantName"),
        "clientType": raw.get("ClientType"),
        "networkAddress": raw.get("NetworkAddress"),
        "events": normalize_events(raw.get("Events")),
    }


def normalize_group_summary(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": raw.get("Name") or "",
        "sessionCount": raw.get("SessionCount") or 0,
        "hasSubgroupExpression": bool(raw.get("HasSubgroupExpression")),
    }


def normalize_session_group(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": raw.get("Name"),
        "sessionType": raw.get("SessionType"),
        "sessionFilter": raw.get("SessionFilter"),
        "subgroupExpressions": raw.get("SubgroupExpressions"),
    }


def _finalize_companies(counts: dict[str, int]) -> list[dict[str, Any]]:
    companies = [
        {"id": raw_id, "name": raw_id.strip(), "sessionCount": count}
        for raw_id, count in counts.items()
        if raw_id.strip()
    ]
    return sorted(companies, key=lambda company: (company["name"].casefold(), company["id"]))


def companies_from_summaries(path_summaries: Any) -> list[dict[str, Any]]:
    """Companies are the LAST non-empty level of PathSessionGroupSummaries."""
    levels = [level for level in path_summaries or [] if level]
    if not levels:
        return []
    counts: dict[str, int] = {}
    for summary in levels[-1]:
        raw_id = summary.get("Name")
        if isinstance(raw_id, str):
            counts[raw_id] = counts.get(raw_id, 0) + int(summary.get("SessionCount") or 0)
    return _finalize_companies(counts)


def companies_from_sessions(sessions: Any) -> list[dict[str, Any]]:
    counts = Counter(
        pad_custom_properties(session.get("CustomPropertyValues"))[0]
        for session in sessions or []
        if session.get("SessionType") == ACCESS_SESSION_TYPE
    )
    return _finalize_companies(dict(counts))


def build_command(command: str, shell: str, timeout_ms: int | None) -> str:
    prefix = ""
    if shell == "powershell":
        prefix += "#!ps\n"
    if timeout_ms is not None:
        prefix += f"#timeout={timeout_ms}\n"
    return prefix + command


def merge_custom_properties(current: list[str], changes: dict[int, str]) -> list[str]:
    merged = pad_custom_properties(current)
    for index, value in changes.items():
        if not 0 <= index < CUSTOM_PROPERTY_COUNT:
            raise ValueError(f"custom property index {index} out of range")
        merged[index] = value
    return merged
