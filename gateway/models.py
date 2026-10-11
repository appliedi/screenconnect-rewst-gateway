from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic.alias_generators import to_camel

from .sc_types import DEFAULT_USER_SOURCE, event_type_value, validate_user_name


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RequestModel(CamelModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ErrorMessage(BaseModel):
    message: str


class AmbiguousCompany(BaseModel):
    message: str
    candidates: list[str] = Field(description="Raw company ids that match case- and whitespace-insensitively")


# ---------- responses ----------


class BackendHealth(CamelModel):
    ok: bool
    enabled: bool


class Health(CamelModel):
    status: Literal["ok", "degraded", "down"]
    restful: BackendHealth
    native: BackendHealth


class Company(CamelModel):
    id: str = Field(description="Raw Custom Property 1 value, exactly as stored (not trimmed). Use this for lookups.")
    name: str = Field(description="Trimmed display name.")
    session_count: int


class Session(CamelModel):
    session_id: str
    name: str | None = None
    session_type: int | None = Field(None, description="0 Support, 1 Meeting, 2 Access")
    guest_machine_name: str | None = None
    guest_logged_on_user_name: str | None = Field(None, description="First signed-in user, if any")
    logged_on_users: list[str] = Field(default_factory=list, description="All signed-in users (e.g. several on a terminal server)")
    operating_system: str | None = None
    company: str = Field(description="Custom Property 1 (raw)")
    site: str = Field(description="Custom Property 2")
    department: str = Field(description="Custom Property 3")
    device_type: str = Field(description="Custom Property 4 (Laptop/Workstation/Server/Domain Controller)")
    custom_properties: list[str] = Field(min_length=8, max_length=8)
    is_online: bool | None = Field(None, description="Agent connected (an active guest connection). Null only if the upstream omits connection data.")
    guest_machine_serial_number: str | None = Field(
        None, description="Restful: GuestInfo.MachineSerialNumber. Native: get_session/details only (null in lists)."
    )
    guest_hardware_network_address: str | None = Field(
        None, description="MAC address. Restful: GuestInfo.HardwareNetworkAddress. Native: get_session/details only."
    )
    guest_client_version: str | None = Field(None, description="ScreenConnect agent version. Both backends.")
    guest_info_update_time: str | None = Field(
        None,
        description="ISO-8601 UTC. When the agent last reported guest info. Native: get_session/details only.",
    )
    last_guest_connected_event_time: str | None = Field(
        None, description="ISO-8601 UTC. Restful only; native has no guest-only equivalent (null)."
    )
    last_guest_disconnected_event_time: str | None = Field(
        None, description="ISO-8601 UTC. Both backends (native is derived from relative seconds, ±2 s)."
    )
    is_deleted: bool | None = Field(None, description="Restful only; null from native.")
    is_ended: bool | None = Field(None, description="Restful only; null from native.")


class SessionEvent(CamelModel):
    event_id: str | None = None
    event_type: int | None = Field(
        None, description="SC.types.SessionEventType, e.g. 44 QueuedCommand, 70 RanCommand (command output), 32 AddedNote"
    )
    time: str | None = Field(None, description="ISO-8601 (native times are converted from ms before BaseTime)")
    host: str | None = None
    data: str | None = None
    connection_id: str | None = None
    correlation_event_id: str | None = None


class Connection(CamelModel):
    connection_id: str | None = None
    process_type: int | None = Field(None, description="1 = host (technician), 2 = guest (agent)")
    participant_name: str | None = None
    client_type: int | None = None
    client_version: str | None = None
    network_address: str | None = None
    attributes: int | None = Field(None, description="Restful only")
    events: list[SessionEvent] = Field(default_factory=list)


class SessionDetails(CamelModel):
    session: Session
    events: list[SessionEvent]
    connections: list[Connection] = Field(default_factory=list)


class ConnectionLogs(CamelModel):
    session_id: str
    connections: list[Connection]


class SessionGroup(CamelModel):
    name: str | None = None
    session_type: int | None = None
    session_filter: str | None = None
    subgroup_expressions: str | list[str] | None = None


class GroupSummary(CamelModel):
    name: str
    session_count: int
    has_subgroup_expression: bool


class GroupSessions(CamelModel):
    groups: list[GroupSummary]
    sessions: list[Session]


class CreatedSession(CamelModel):
    session_id: str
    name: str
    raw: dict[str, Any] | None = Field(None, description="Upstream CreateSession response, verbatim")


class SavedUser(CamelModel):
    user_name: str
    old_user_name: str | None = Field(None, description="Set when an existing user was updated or renamed")
    created: bool


class QueuedEvents(CamelModel):
    queued: int
    session_ids: list[str]


class RawResponse(CamelModel):
    """Upstream payload, verbatim, with source metadata."""

    backend: Literal["restful", "native"]
    upstream_method: str
    received_at: str = Field(
        description="ISO-8601 UTC. Native times in the payload are relative (seconds/ms before this or BaseTime)"
    )
    omitted_fields: list[str] = Field(
        description="Object keys removed at every depth because they are agent credentials "
        "(GuestPublicKey, WakeToken, ClientMachineCookie). Nothing else is removed."
    )
    response: Any = Field(description="What ScreenConnect returned, minus omittedFields")


class QueuedResult(CamelModel):
    session_id: str
    queued: bool


class OkResult(CamelModel):
    session_id: str
    ok: bool


class RenamedSession(CamelModel):
    session_id: str
    name: str


class CustomPropertiesResult(CamelModel):
    session_id: str
    custom_properties: list[str] = Field(min_length=8, max_length=8)


class CreatedUser(CamelModel):
    user_name: str
    created: bool


# ---------- requests ----------


class CustomPropertyFields(RequestModel):
    company: str | None = Field(None, description="Custom Property 1")
    site: str | None = Field(None, description="Custom Property 2")
    department: str | None = Field(None, description="Custom Property 3")
    device_type: str | None = Field(None, description="Custom Property 4 (Laptop/Workstation/Server/Domain Controller)")

    def named_changes(self) -> dict[int, str]:
        values = (self.company, self.site, self.department, self.device_type)
        return {index: value for index, value in enumerate(values) if value is not None}


class IndexedCustomProperties(BaseModel):
    """Custom properties by zero-based index ("0" = Custom Property 1 ... "7" = Custom Property 8)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    p0: str | None = Field(None, alias="0")
    p1: str | None = Field(None, alias="1")
    p2: str | None = Field(None, alias="2")
    p3: str | None = Field(None, alias="3")
    p4: str | None = Field(None, alias="4")
    p5: str | None = Field(None, alias="5")
    p6: str | None = Field(None, alias="6")
    p7: str | None = Field(None, alias="7")

    def changes(self) -> dict[int, str]:
        values = [self.p0, self.p1, self.p2, self.p3, self.p4, self.p5, self.p6, self.p7]
        return {index: value for index, value in enumerate(values) if value is not None}


class WithCustomProperties(CustomPropertyFields):
    custom_properties: IndexedCustomProperties | None = Field(
        None,
        description='Any of the 8 properties by index ("0"-"7"). A named field (company/site/department/deviceType) '
        "and its index may both be given only with the same value; different values are rejected (400).",
    )

    @model_validator(mode="after")
    def _no_conflicts(self):
        indexed = self.custom_properties.changes() if self.custom_properties else {}
        for index, value in self.named_changes().items():
            if index in indexed and indexed[index] != value:
                raise ValueError(f"custom property index {index} conflicts with its named field")
        return self

    def changes(self) -> dict[int, str]:
        changes = self.custom_properties.changes() if self.custom_properties else {}
        changes.update(self.named_changes())
        return changes


class NativeTarget(RequestModel):
    group_path: list[str] | None = Field(
        None,
        min_length=1,
        description="Native session group path (sessionGroupPathParts), e.g. [\"All Machines by Company\", \"Acme\"]. "
        "Default: the group ScreenConnect reports for the session.",
    )
    session_type: int | None = Field(None, ge=0, le=2, description="Native lookup hint: 0 Support, 1 Meeting, 2 Access")


class CreateSessionRequest(WithCustomProperties):
    session_type: int = Field(ge=0, le=2, description="0 Support, 1 Meeting, 2 Access")
    name: str = Field(min_length=1)
    is_public: bool = False
    code: str | None = None


class CommandRequest(RequestModel):
    command: str = Field(min_length=1)
    shell: Literal["cmd", "powershell"] = "cmd"
    timeout_ms: int | None = Field(None, ge=1, le=86_400_000)
    group: str | None = Field(None, description="Native fallback: single session group name (same as groupPath=[group])")
    group_path: list[str] | None = Field(None, min_length=1, description="Native fallback: session group path")
    session_type: int | None = Field(None, ge=0, le=2, description="Native lookup hint")

    @model_validator(mode="after")
    def _one_group(self):
        if self.group is not None and self.group_path is not None:
            raise ValueError("give either group or groupPath, not both")
        return self


class MessageRequest(RequestModel):
    sender: str = Field(alias="from", min_length=1)
    message: str = Field(min_length=1)


class NoteRequest(RequestModel):
    note: str = Field(min_length=1)


class ToolboxRequest(RequestModel):
    item_name: str = Field(min_length=1)


class RenameRequest(RequestModel):
    name: str = Field(min_length=1)


class CustomPropertiesUpdate(WithCustomProperties):
    group_path: list[str] | None = Field(
        None, min_length=1, description="Native: sessionGroupPathParts for UpdateSessionCustomPropertyValue"
    )
    session_type: int | None = Field(None, ge=0, le=2, description="Native lookup hint")


OTP_DESCRIPTION = (
    "SaveUser passwordQuestion argument, which ScreenConnect uses for the user's MFA/OTP setting: "
    '"email", "<provider>:<value>", or null for none. Alias: otpTokenString.'
)


class CreateUserRequest(RequestModel):
    user_name: str = Field(min_length=1)
    password: SecretStr
    display_name: str = ""
    email: str = ""
    roles: list[str] = Field(default_factory=list)
    force_password_change: bool = False
    user_source: str = Field(DEFAULT_USER_SOURCE, min_length=1)
    comment: str = ""
    password_question: str | None = Field(
        None, validation_alias=AliasChoices("passwordQuestion", "otpTokenString"), description=OTP_DESCRIPTION
    )

    @field_validator("user_name")
    @classmethod
    def _clean_user_name(cls, value: str) -> str:
        return validate_user_name(value)


class SaveUserRequest(RequestModel):
    """Mirrors SecurityService.SaveUser. oldUserName null creates a user; set it to update or rename one."""

    user_source: str = Field(DEFAULT_USER_SOURCE, min_length=1, description="userSourceName")
    old_user_name: str | None = Field(
        None,
        validation_alias=AliasChoices("oldUserName", "originalUserName"),
        description="Existing user to update/rename (originalUserName). Null creates a new user.",
    )
    new_user_name: str = Field(min_length=1, description="Resulting user name (a different value renames)")
    password: SecretStr | None = Field(None, description="Null keeps the existing password (updates only)")
    verify_password: SecretStr | None = Field(None, description="Defaults to password; must match it if given")
    password_question: str | None = Field(
        None, validation_alias=AliasChoices("passwordQuestion", "otpTokenString"), description=OTP_DESCRIPTION
    )
    display_name: str = ""
    comment: str = ""
    email: str = ""
    roles: list[str] = Field(default_factory=list, description="roleNames")
    force_password_change: bool = False

    @field_validator("old_user_name", "new_user_name")
    @classmethod
    def _clean_user_names(cls, value: str | None) -> str | None:
        return validate_user_name(value)

    @model_validator(mode="after")
    def _check(self):
        if self.old_user_name is None and self.password is None:
            raise ValueError("password is required when creating a user")
        if self.verify_password is not None and (
            self.password is None or self.verify_password.get_secret_value() != self.password.get_secret_value()
        ):
            raise ValueError("verifyPassword does not match password")
        if self.old_user_name is not None:
            # SaveUser replaces every field, so an update must state them all; a silent default
            # would wipe the user's display name, roles or MFA setting.
            required = {"display_name", "comment", "email", "roles", "password_question", "force_password_change"}
            missing = sorted(required - self.model_fields_set)
            if missing:
                raise ValueError(
                    "updating a user replaces all fields; also provide: " + ", ".join(to_camel(f) for f in missing)
                )
        return self


class SessionEventEntry(RequestModel):
    session_id: str = Field(pattern=r"^[0-9a-fA-F-]{32,36}$")
    event_type: int | str = Field(description="SC.types.SessionEventType name or number, e.g. QueuedCommand / 44")
    data: str | None = None
    connection_id: str | None = None
    correlation_event_id: str | None = None

    @field_validator("event_type")
    @classmethod
    def _known_type(cls, value: int | str) -> int:
        return event_type_value(value)


class SessionEventsRequest(RequestModel):
    group_path: list[str] = Field(min_length=1, description="sessionGroupPathParts the sessions belong to")
    events: list[SessionEventEntry] = Field(min_length=1, max_length=100)
