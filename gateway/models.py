from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic.alias_generators import to_camel


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
    guest_logged_on_user_name: str | None = None
    operating_system: str | None = None
    company: str = Field(description="Custom Property 1 (raw)")
    site: str = Field(description="Custom Property 2")
    department: str = Field(description="Custom Property 3")
    device_type: str = Field(description="Custom Property 4 (Laptop/Workstation/Server/Domain Controller)")
    custom_properties: list[str] = Field(min_length=8, max_length=8)
    is_online: bool | None = Field(None, description="Agent connected (an active guest connection). Null only if the upstream omits connection data.")


class SessionEvent(CamelModel):
    event_id: str | None = None
    event_type: int | None = Field(None, description="44 RanCommand, 70 ProcessedCommand (command output)")
    time: str | None = None
    data: str | None = None


class SessionDetails(CamelModel):
    session: Session
    events: list[SessionEvent]


class Connection(CamelModel):
    connection_id: str | None = None
    process_type: int | None = Field(None, description="2 = agent (guest) process")
    participant_name: str | None = None
    client_type: int | None = None
    network_address: str | None = None
    events: list[SessionEvent]


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


class ExtraCustomProperties(BaseModel):
    """Custom Properties 5-8, keyed by zero-based index."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    p4: str | None = Field(None, alias="4")
    p5: str | None = Field(None, alias="5")
    p6: str | None = Field(None, alias="6")
    p7: str | None = Field(None, alias="7")

    def changes(self) -> dict[int, str]:
        values = {4: self.p4, 5: self.p5, 6: self.p6, 7: self.p7}
        return {index: value for index, value in values.items() if value is not None}


class CreateSessionRequest(CustomPropertyFields):
    session_type: int = Field(ge=0, le=2, description="0 Support, 1 Meeting, 2 Access")
    name: str = Field(min_length=1)
    is_public: bool = False
    code: str | None = None


class CommandRequest(RequestModel):
    command: str = Field(min_length=1)
    shell: Literal["cmd", "powershell"] = "cmd"
    timeout_ms: int | None = Field(None, ge=1, le=86_400_000)
    group: str | None = Field(None, description="Session group for the native fallback (default 'All Machines')")


class MessageRequest(RequestModel):
    sender: str = Field(alias="from", min_length=1)
    message: str = Field(min_length=1)


class NoteRequest(RequestModel):
    note: str = Field(min_length=1)


class ToolboxRequest(RequestModel):
    item_name: str = Field(min_length=1)


class RenameRequest(RequestModel):
    name: str = Field(min_length=1)


class CustomPropertiesUpdate(CustomPropertyFields):
    custom_properties: ExtraCustomProperties | None = None

    def changes(self) -> dict[int, str]:
        changes = self.named_changes()
        if self.custom_properties is not None:
            changes.update(self.custom_properties.changes())
        return changes


class CreateUserRequest(RequestModel):
    user_name: str = Field(min_length=1)
    password: SecretStr
    display_name: str = ""
    email: str = ""
    roles: list[str] = Field(default_factory=list)
    force_password_change: bool = False
