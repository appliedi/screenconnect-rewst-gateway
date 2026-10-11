"""ScreenConnect enums, copied verbatim from the web client's SC.types definitions."""

from __future__ import annotations

SESSION_TYPES = {"Support": 0, "Meeting": 1, "Access": 2}

# SC.types.SessionEventType
SESSION_EVENT_TYPES: dict[str, int] = {
    "None": 0,
    "DeletedEvent": 1,
    "ProcessedEvent": 2,
    "AcknowledgedEvent": 3,
    "AnnotatedEvent": 4,
    "SuccessResult": 5,
    "FailureResult": 6,
    "Connected": 10,
    "Disconnected": 11,
    "CreatedSession": 20,
    "DeletedSession": 21,
    "InitiatedJoin": 30,
    "InvitedGuest": 31,
    "AddedNote": 32,
    "QueuedReinstall": 40,
    "QueuedUninstall": 41,
    "QueuedInvalidateLicense": 42,
    "QueuedWake": 43,
    "QueuedCommand": 44,
    "QueuedMessage": 45,
    "QueuedGuestInfoUpdate": 46,
    "QueuedTool": 47,
    "QueuedForceDisconnect": 48,
    "QueuedInstallAccess": 49,
    "ModifiedName": 60,
    "ModifiedIsPublic": 61,
    "ModifiedCode": 62,
    "ModifiedHost": 63,
    "ModifiedCustomProperty": 64,
    "ModifiedGuestInfo": 65,
    "RanCommand": 70,
    "SentMessage": 71,
    "SentPrintJob": 80,
    "ReceivedPrintJob": 81,
    "CopiedText": 82,
    "CopiedFiles": 83,
    "DraggedFiles": 84,
    "RanFiles": 85,
    "SentFiles": 86,
    "SwitchedLogonSession": 87,
    "RanAutoReinstall": 88,
    "ResetRegisteredGuestClient": 89,
    "SentText": 90,
    "SentBlob": 91,
    "QueuedUninstallAndDelete": 100,
    "QueuedElevatedTool": 101,
    "QueuedProceedElevation": 102,
    "QueuedProceedAdministrativeLogon": 103,
    "QueuedLogonSessionLogoff": 104,
    "QueuedLogonSessionLock": 105,
    "EncounteredElevationPrompt": 110,
    "RequestedElevation": 111,
    "RequestedAdministrativeLogon": 112,
    "ApprovedRequest": 113,
    "DeniedRequest": 114,
    "CanceledRequest": 115,
    "EncounteredLogonScreen": 116,
    "QueuedSilentElevatedTool": 117,
    "RequestTimedOut": 118,
    "StartedCapture": 120,
    "EndedCapture": 121,
    "ConsentGranted": 122,
    "ConsentRefused": 123,
    "ModifiedSessionAttributes": 124,
}
SESSION_EVENT_TYPE_VALUES = frozenset(SESSION_EVENT_TYPES.values())

# add_session_events default: nothing beyond what send_command/send_message/add_note/
# run_toolbox_item already allow. Uninstall, reinstall, delete, license, disconnect,
# elevation and logon types must be opted into with ALLOWED_EVENT_TYPES.
DEFAULT_ALLOWED_EVENT_TYPES = frozenset(
    SESSION_EVENT_TYPES[name]
    for name in (
        "AcknowledgedEvent",
        "AddedNote",
        "QueuedWake",
        "QueuedCommand",
        "QueuedMessage",
        "QueuedGuestInfoUpdate",
        "QueuedTool",
    )
)

# SC.types.SessionDetailsItems (bit flags for GetSessionDetails' third argument)
DETAILS_SESSION_INFO = 1
DETAILS_GUEST_SCREENSHOT = 4
DETAILS_EVENTS = 8
DETAILS_CONNECTIONS = 16
DETAILS_ALL = -1

DEFAULT_USER_SOURCE = "InternalMembershipProvider"


def event_type_value(value: int | str) -> int:
    """Accept an enum name or number; reject anything ScreenConnect doesn't define."""
    if isinstance(value, str):
        if value in SESSION_EVENT_TYPES:
            return SESSION_EVENT_TYPES[value]
        if value.isdigit():
            value = int(value)
        else:
            raise ValueError(f"unknown session event type '{value}'")
    if value not in SESSION_EVENT_TYPE_VALUES:
        raise ValueError(f"unknown session event type {value}")
    return value
