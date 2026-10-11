"""Request/response parity tests for the 16 original Rewst ScreenConnect operations.

Upstream argument shapes come from ScreenConnect's own web client (SC.service definitions and
the admin/host UI call sites); payload shapes mirror live responses."""

import json
import logging

import httpx
import pytest
from conftest import READ, SESSION_ID, WRITE, raw_session

SUPPORT_ID = "22222222-0000-0000-0000-000000000000"
MEETING_ID = "33333333-0000-0000-0000-000000000000"
GROUP_PATH_BY_TYPE = {0: ["All Sessions"], 1: ["All Meetings"], 2: ["All Machines by Company"]}


def live(sessions, *, path=None, untruncated=None, extra=None):
    info = {
        "Sessions": sessions,
        "PathSessionGroupSummaries": [[{"Name": "All Machines", "SessionCount": len(sessions), "HasSubgroupExpression": False}]],
        "AlternateSessionGroupSummaries": [],
        "SessionGroupPath": path or [],
        "Filter": "",
        "UntruncatedSessionCount": len(sessions) if untruncated is None else untruncated,
    }
    info.update(extra or {})
    return {
        "Version": 123,
        "ProductVersion": "25.4.16.9293",
        "ResponseInfoMap": {"HostSessionInfo": info, "ActionCenterInfo": {"ActionItems": []}},
    }


def typed_world(fake, sessions):
    """GetLiveData that, like ScreenConnect, only finds a session under its own session type."""

    def handler(args):
        info = args[0]["HostSessionInfo"]
        of_type = [s for s in sessions if s["SessionType"] == info["sessionType"]]
        if info["findSessionID"]:
            of_type = [s for s in of_type if s["SessionID"] == info["findSessionID"]]
        path = info["sessionGroupPathParts"] or GROUP_PATH_BY_TYPE[info["sessionType"]]
        return live(of_type[: info["sessionLimit"]], path=path, untruncated=len(of_type))

    fake.native["PageService.ashx/GetLiveData"] = handler


def host_info_requests(fake):
    return [args[0]["HostSessionInfo"] for args in fake.calls_to("native", "PageService.ashx/GetLiveData")]


# ======================= RESTful API Manager =======================


def test_01_add_note_to_session(client, fake):
    fake.restful["AddNoteToSession"] = lambda args: None
    response = client.post(f"/v1/sessions/{SESSION_ID}/notes", json={"note": "Checked disk"}, headers=WRITE)
    assert response.status_code == 200 and response.json() == {"sessionId": SESSION_ID, "ok": True}
    assert fake.calls_to("restful", "AddNoteToSession") == [[SESSION_ID, "Checked disk"]]


@pytest.mark.parametrize("session_type", [0, 1, 2])
def test_02_create_session_all_types_and_eight_properties(client, fake, session_type):
    upstream = {"SessionID": SESSION_ID, "Name": "New", "SessionType": session_type, "IsPublic": True}
    fake.restful["CreateSession"] = lambda args: upstream
    body = {
        "sessionType": session_type,
        "name": "New",
        "isPublic": True,
        "code": "1234",
        "company": "Acme",
        "deviceType": "Laptop",
        "customProperties": {"0": "Acme", "1": "HQ", "2": "IT", "4": "p5", "5": "p6", "6": "p7", "7": "p8"},
    }
    response = client.post("/v1/sessions", json=body, headers=WRITE)
    assert response.status_code == 201
    assert response.json() == {"sessionId": SESSION_ID, "name": "New", "raw": upstream}
    assert fake.calls_to("restful", "CreateSession") == [
        [session_type, "New", True, "1234", ["Acme", "HQ", "IT", "Laptop", "p5", "p6", "p7", "p8"]]
    ]


def test_02_create_session_rejects_conflicting_named_and_indexed_values(client, fake):
    body = {"sessionType": 2, "name": "x", "site": "HQ", "customProperties": {"1": "Branch"}}
    response = client.post("/v1/sessions", json=body, headers=WRITE)
    assert response.status_code == 400
    assert "custom property index 1 conflicts" in response.json()["message"]
    assert fake.calls == []


def test_03_get_session_by_session_id(client, fake):
    upstream = raw_session(properties=["Acme", "HQ", "", "Server", "", "", "", ""])
    upstream["FutureUpstreamField"] = {"nested": [1, 2]}
    fake.restful["GetSessionBySessionID"] = lambda args: [upstream]
    normalized = client.get(f"/v1/sessions/{SESSION_ID}", headers=READ).json()
    assert normalized["sessionId"] == SESSION_ID and normalized["deviceType"] == "Server"
    raw = client.get(f"/v1/raw/restful/sessions/{SESSION_ID}", headers=READ)
    assert raw.headers["X-Gateway-Backend"] == "restful"
    assert raw.json()["upstreamMethod"] == "GetSessionBySessionID"
    assert raw.json()["response"] == [upstream]  # verbatim, unknown fields included
    assert fake.calls_to("restful", "GetSessionBySessionID") == [[SESSION_ID], [SESSION_ID]]


def test_04_get_session_details_by_session_id(client, fake):
    payload = {
        "Session": raw_session(),
        "Events": [
            {"EventID": "e1", "EventType": 44, "Time": "2026-10-11T10:00:00Z", "Host": "tech", "Data": "whoami",
             "ConnectionID": "c1", "CorrelationEventID": None},
            {"EventID": "e2", "EventType": 70, "Time": "2026-10-11T10:00:02Z", "Host": "", "Data": "acme\\svc",
             "ConnectionID": "c2", "CorrelationEventID": "e1"},
        ],
        "Connections": [
            {"ConnectionID": "c2", "ProcessType": 2, "ClientType": 1, "ClientVersion": "25.4", "NetworkAddress": "10.0.0.5",
             "ParticipantName": "", "Attributes": 0, "ClientMachineCookie": "cookie"}
        ],
        "GuestScreenshotContent": "iVBORw0KGgo=",
    }
    fake.restful["GetSessionDetailsBySessionID"] = lambda args: payload
    body = client.get(f"/v1/sessions/{SESSION_ID}/details", headers=READ).json()
    assert body["events"][1] == {
        "eventId": "e2", "eventType": 70, "time": "2026-10-11T10:00:02Z", "host": "",
        "data": "acme\\svc", "connectionId": "c2", "correlationEventId": "e1",
    }
    assert body["connections"] == [
        {"connectionId": "c2", "processType": 2, "participantName": "", "clientType": 1, "clientVersion": "25.4",
         "networkAddress": "10.0.0.5", "attributes": 0, "events": []}
    ]
    raw = client.get(f"/v1/raw/restful/sessions/{SESSION_ID}/details", headers=READ).json()
    assert raw["response"] == payload


def test_05_get_sessions_by_filter(client, fake):
    fake.restful["GetSessionsByFilter"] = lambda args: [raw_session(), raw_session("s0", session_type=0)]
    response = client.get("/v1/sessions", params={"filter": "GuestMachineName LIKE 'PC*'"}, headers=READ)
    assert [s["sessionType"] for s in response.json()] == [2, 0]  # no sessionType: all types
    assert response.headers["X-Gateway-Truncated"] == "unknown"  # restful gives no signal
    raw = client.get("/v1/raw/restful/sessions", params={"filter": "GuestMachineName LIKE 'PC*'"}, headers=READ).json()
    assert raw["upstreamMethod"] == "GetSessionsByFilter" and len(raw["response"]) == 2
    assert fake.calls_to("restful", "GetSessionsByFilter") == [["GuestMachineName LIKE 'PC*'"]] * 2


def test_06_get_sessions_by_name(client, fake):
    fake.restful["GetSessionsByName"] = lambda args: [raw_session(name="PC-01")]
    assert client.get("/v1/sessions", params={"name": "PC-01"}, headers=READ).json()[0]["name"] == "PC-01"
    raw = client.get("/v1/raw/restful/sessions", params={"name": "PC-01"}, headers=READ).json()
    assert raw["upstreamMethod"] == "GetSessionsByName"
    assert fake.calls_to("restful", "GetSessionsByName") == [["PC-01"], ["PC-01"]]


def test_07_send_command_to_session(client, fake):
    fake.restful["SendCommandToSession"] = lambda args: None
    body = {"command": "Get-Date", "shell": "powershell", "timeoutMs": 30000}
    response = client.post(f"/v1/sessions/{SESSION_ID}/commands", json=body, headers=WRITE)
    assert response.status_code == 202
    assert fake.calls_to("restful", "SendCommandToSession") == [[SESSION_ID, "#!ps\n#timeout=30000\nGet-Date"]]


def test_08_send_message_to_session(client, fake):
    fake.restful["SendMessageToSession"] = lambda args: None
    response = client.post(f"/v1/sessions/{SESSION_ID}/messages", json={"from": "IT", "message": "Reboot at 5"}, headers=WRITE)
    assert response.status_code == 202
    assert fake.calls_to("restful", "SendMessageToSession") == [[SESSION_ID, "IT", "Reboot at 5"]]  # byHost


def test_09_send_toolbox_item_to_session(client, fake):
    fake.restful["SendToolboxItemToSession"] = lambda args: None
    response = client.post(f"/v1/sessions/{SESSION_ID}/toolbox", json={"itemName": "Cleanup"}, headers=WRITE)
    assert response.status_code == 202
    assert fake.calls_to("restful", "SendToolboxItemToSession") == [[SESSION_ID, "Cleanup"]]


def test_10_update_session_custom_properties_all_eight(client, fake):
    fake.restful["GetSessionBySessionID"] = lambda args: [raw_session(properties=["a", "b", "c", "d", "e", "f", "g", "h"])]
    fake.restful["UpdateSessionCustomProperties"] = lambda args: None
    body = {"customProperties": {"0": "A", "3": "D", "7": "H"}, "site": "B"}
    response = client.patch(f"/v1/sessions/{SESSION_ID}/custom-properties", json=body, headers=WRITE)
    assert response.json()["customProperties"] == ["A", "B", "c", "D", "e", "f", "g", "H"]
    assert fake.calls_to("restful", "UpdateSessionCustomProperties") == [
        [SESSION_ID, ["A", "B", "c", "D", "e", "f", "g", "H"]]
    ]


def test_11_update_session_name(client, fake):
    fake.restful["UpdateSessionName"] = lambda args: None
    response = client.patch(f"/v1/sessions/{SESSION_ID}/name", json={"name": "PC-02"}, headers=WRITE)
    assert response.json() == {"sessionId": SESSION_ID, "name": "PC-02"}
    assert fake.calls_to("restful", "UpdateSessionName") == [[SESSION_ID, "PC-02"]]


# ======================= Native ScreenConnect =======================


def test_12_add_session_events_batch(client, fake):
    fake.native["PageService.ashx/AddSessionEvents"] = lambda args: None
    body = {
        "groupPath": ["All Machines by Company", "Acme"],
        "events": [
            {"sessionId": SESSION_ID, "eventType": "QueuedCommand", "data": "ipconfig"},
            {"sessionId": SUPPORT_ID, "eventType": 45, "data": "Hello"},
            {"sessionId": SESSION_ID, "eventType": "AcknowledgedEvent", "connectionId": "c1", "correlationEventId": "e9"},
        ],
    }
    response = client.post("/v1/session-events", json=body, headers=WRITE)
    assert response.status_code == 202
    assert response.json() == {"queued": 3, "sessionIds": [SESSION_ID, SUPPORT_ID]}
    assert fake.calls_to("native", "PageService.ashx/AddSessionEvents") == [
        [
            ["All Machines by Company", "Acme"],
            [
                {"SessionID": SESSION_ID, "EventType": 44, "Data": "ipconfig"},
                {"SessionID": SUPPORT_ID, "EventType": 45, "Data": "Hello"},
                {"SessionID": SESSION_ID, "EventType": 3, "ConnectionID": "c1", "CorrelationEventID": "e9"},
            ],
        ]
    ]


@pytest.mark.parametrize(
    "event",
    [{"eventType": "MadeUpEvent"}, {"eventType": 999}],
)
def test_12_add_session_events_rejects_unknown_types(client, fake, event):
    body = {"groupPath": ["All Machines"], "events": [{"sessionId": SESSION_ID, **event}]}
    assert client.post("/v1/session-events", json=body, headers=WRITE).status_code == 400
    assert fake.calls == []


def test_12_add_session_events_limits(make_client, fake):
    client = make_client(allowed_event_types=frozenset({44}))
    uninstall = {"groupPath": ["All Machines"], "events": [{"sessionId": SESSION_ID, "eventType": "QueuedUninstall"}]}
    response = client.post("/v1/session-events", json=uninstall, headers=WRITE)
    assert response.status_code == 403 and response.json() == {"message": "event types not allowed: 41"}
    too_many = {"groupPath": ["x"], "events": [{"sessionId": SESSION_ID, "eventType": 44}] * 101}
    assert client.post("/v1/session-events", json=too_many, headers=WRITE).status_code == 400
    assert client.post("/v1/session-events", json=uninstall, headers=READ).status_code == 403
    assert fake.calls_to("native", "PageService.ashx/AddSessionEvents") == []


def test_12_destructive_event_types_are_opt_in(make_client, fake):
    fake.native["PageService.ashx/AddSessionEvents"] = lambda args: None
    uninstall = {"groupPath": ["All Machines"], "events": [{"sessionId": SESSION_ID, "eventType": "QueuedUninstallAndDelete"}]}
    default = make_client()
    response = default.post("/v1/session-events", json=uninstall, headers=WRITE)
    assert response.status_code == 403 and response.json() == {"message": "event types not allowed: 100"}
    from gateway.sc_types import SESSION_EVENT_TYPE_VALUES

    opted_in = make_client(allowed_event_types=SESSION_EVENT_TYPE_VALUES)
    assert opted_in.post("/v1/session-events", json=uninstall, headers=WRITE).status_code == 202


def test_12_allowed_event_types_config():
    from gateway.config import Settings
    from gateway.sc_types import DEFAULT_ALLOWED_EVENT_TYPES, SESSION_EVENT_TYPE_VALUES

    base = {"GATEWAY_WRITE_KEY": "w-key-123456"}
    assert Settings.from_env(base).allowed_event_types == DEFAULT_ALLOWED_EVENT_TYPES
    assert Settings.from_env({**base, "ALLOWED_EVENT_TYPES": "all"}).allowed_event_types == SESSION_EVENT_TYPE_VALUES
    assert Settings.from_env({**base, "ALLOWED_EVENT_TYPES": "QueuedCommand, 45"}).allowed_event_types == {44, 45}
    assert 41 not in DEFAULT_ALLOWED_EVENT_TYPES and 100 not in DEFAULT_ALLOWED_EVENT_TYPES


def test_12_native_command_uses_caller_group_path(client, fake):
    fake.restful["SendCommandToSession"] = lambda args: httpx.Response(401)
    fake.native["PageService.ashx/AddSessionEvents"] = lambda args: None
    body = {"command": "dir", "groupPath": ["All Machines by Company", "Acme"]}
    assert client.post(f"/v1/sessions/{SESSION_ID}/commands", json=body, headers=WRITE).status_code == 202
    path, events = fake.calls_to("native", "PageService.ashx/AddSessionEvents")[0]
    assert path == ["All Machines by Company", "Acme"] and events[0]["EventType"] == 44
    assert fake.calls_to("native", "PageService.ashx/GetLiveData") == []  # no lookup needed


@pytest.mark.parametrize("session_type", [0, 1, 2])
def test_13_get_live_data_request_and_full_response(client, fake, session_type):
    upstream = live([raw_session()], path=["Root", "Child"], extra={"AlternateSessionGroupSummaries": [[{"Name": "x"}]]})
    fake.native["PageService.ashx/GetLiveData"] = lambda args: upstream
    params = [("sessionType", session_type), ("path", "Root"), ("path", "Child"), ("filter", "PC"),
              ("findSessionId", SESSION_ID), ("limit", 25)]
    response = client.get("/v1/raw/native/live-data", params=params, headers=READ)
    body = response.json()
    assert body["backend"] == "native" and body["upstreamMethod"] == "PageService.ashx/GetLiveData"
    assert body["receivedAt"] == "2026-10-11T12:00:00Z"
    assert body["response"] == upstream  # every hierarchy level preserved
    assert fake.calls_to("native", "PageService.ashx/GetLiveData") == [[
        {"HostSessionInfo": {"sessionType": session_type, "sessionGroupPathParts": ["Root", "Child"], "filter": "PC",
                             "findSessionID": SESSION_ID, "sessionLimit": 25}, "ActionCenterInfo": {}},
        0,
    ]]


def test_13_group_sessions_truncation_and_complete(client, fake):
    sessions = [raw_session(f"s{i}") for i in range(5)]
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live(
        sessions[: args[0]["HostSessionInfo"]["sessionLimit"]], untruncated=5
    )
    capped = client.get("/v1/session-groups/sessions", params={"path": "All Machines", "limit": 2}, headers=READ)
    assert capped.headers["X-Gateway-Truncated"] == "true" and capped.headers["X-Gateway-Total-Count"] == "5"
    assert len(capped.json()["sessions"]) == 2
    full = client.get("/v1/session-groups/sessions", params={"path": "All Machines", "limit": 2, "complete": True}, headers=READ)
    assert full.headers["X-Gateway-Truncated"] == "false" and len(full.json()["sessions"]) == 5
    assert [r["sessionLimit"] for r in host_info_requests(fake)] == [2, 2, 5]


def test_13_group_sessions_nested_path_type_and_find(client, fake):
    fake.native["PageService.ashx/GetLiveData"] = lambda args: live([raw_session("s", session_type=0)])
    params = [("path", "All Sessions"), ("path", "Today"), ("sessionType", 0), ("findSessionId", SUPPORT_ID)]
    assert client.get("/v1/session-groups/sessions", params=params, headers=READ).status_code == 200
    request = host_info_requests(fake)[0]
    assert (request["sessionGroupPathParts"], request["sessionType"], request["findSessionID"]) == (
        ["All Sessions", "Today"], 0, SUPPORT_ID
    )


def test_14_get_session_details_native_finds_support_session(make_client, fake):
    client = make_client(backend_preference="native-first")
    typed_world(fake, [raw_session(), raw_session(SUPPORT_ID, name="Support 1", session_type=0)])
    fake.native["PageService.ashx/GetSessionDetails"] = lambda args: {
        "BaseTime": 1760184000000, "Session": {"GuestMachineSerialNumber": "SN1"}, "Events": [], "Connections": []
    }
    body = client.get(f"/v1/sessions/{SUPPORT_ID}/details", headers=READ).json()
    assert body["session"]["sessionId"] == SUPPORT_ID and body["session"]["sessionType"] == 0
    # Access tried first, then Support: no Access-only assumption.
    assert [(r["sessionType"], r["findSessionID"]) for r in host_info_requests(fake)] == [(2, SUPPORT_ID), (0, SUPPORT_ID)]
    # Path comes from ScreenConnect's SessionGroupPath; items = SessionInfo|Events|Connections (no screenshot).
    assert fake.calls_to("native", "PageService.ashx/GetSessionDetails") == [[["All Sessions"], SUPPORT_ID, 25]]


def test_14_get_session_meeting_with_type_hint(make_client, fake):
    client = make_client(backend_preference="native-first")
    typed_world(fake, [raw_session(MEETING_ID, name="Standup", session_type=1)])
    fake.native["PageService.ashx/GetSessionDetails"] = lambda args: {"BaseTime": 1760184000000, "Session": {}}
    response = client.get(f"/v1/sessions/{MEETING_ID}", params={"sessionType": 1}, headers=READ)
    assert response.json()["sessionType"] == 1
    assert [r["sessionType"] for r in host_info_requests(fake)] == [1]  # one targeted lookup
    assert fake.calls_to("native", "PageService.ashx/GetSessionDetails") == [[["All Meetings"], MEETING_ID, 1]]


def test_14_raw_native_details_items_and_path(client, fake):
    upstream = {"BaseTime": 1760184000000, "Session": {"GuestMachineName": "PC"}, "GuestScreenshotContent": "AAA"}
    fake.native["PageService.ashx/GetSessionDetails"] = lambda args: upstream
    params = [("groupPath", "All Machines"), ("items", -1)]
    body = client.get(f"/v1/raw/native/sessions/{SESSION_ID}/details", params=params, headers=READ).json()
    assert body["response"] == upstream
    assert fake.calls_to("native", "PageService.ashx/GetSessionDetails") == [[["All Machines"], SESSION_ID, -1]]


def test_15_update_custom_property_value_preserves_caller_path(client, fake):
    fake.restful["GetSessionBySessionID"] = lambda args: httpx.Response(401)
    typed_world(fake, [raw_session(properties=["Acme", "", "", "", "", "", "", ""])])
    fake.native["PageService.ashx/UpdateSessionCustomPropertyValue"] = lambda args: None
    body = {"customProperties": {"5": "tag"}, "groupPath": ["All Machines by Company", "Acme"]}
    response = client.patch(f"/v1/sessions/{SESSION_ID}/custom-properties", json=body, headers=WRITE)
    assert response.status_code == 200 and response.headers["X-Gateway-Backend"] == "native"
    assert fake.calls_to("native", "PageService.ashx/UpdateSessionCustomPropertyValue") == [
        [["All Machines by Company", "Acme"], SESSION_ID, 5, "tag"]
    ]


def test_15_update_custom_property_value_support_session_default_path(make_client, fake):
    client = make_client(backend_preference="native-first")
    typed_world(fake, [raw_session(SUPPORT_ID, session_type=0, properties=[""] * 8)])
    fake.native["PageService.ashx/UpdateSessionCustomPropertyValue"] = lambda args: None
    response = client.patch(f"/v1/sessions/{SUPPORT_ID}/custom-properties", json={"company": "Acme"}, headers=WRITE)
    assert response.status_code == 200
    assert fake.calls_to("native", "PageService.ashx/UpdateSessionCustomPropertyValue") == [
        [["All Sessions"], SUPPORT_ID, 0, "Acme"]
    ]


def test_16_save_user_create(client, fake):
    fake.native["SecurityService.ashx/SaveUser"] = lambda args: None
    body = {"newUserName": "tech1", "password": "Pw-123456!", "otpTokenString": "email", "displayName": "Tech",
            "comment": "contractor", "email": "t@example.com", "roles": ["Technician"], "forcePasswordChange": True}
    response = client.post("/v1/users/save", json=body, headers=WRITE)
    assert response.json() == {"userName": "tech1", "oldUserName": None, "created": True}
    assert fake.calls_to("native", "SecurityService.ashx/SaveUser") == [
        ["InternalMembershipProvider", None, "tech1", "Pw-123456!", "Pw-123456!", "email", "Tech", "contractor",
         "t@example.com", ["Technician"], True]
    ]


def test_16_save_user_update_keeps_password_and_renames(client, fake, caplog):
    fake.native["SecurityService.ashx/SaveUser"] = lambda args: None
    body = {"userSource": "InternalMembershipProvider", "oldUserName": "tech1", "newUserName": "tech-one",
            "password": None, "passwordQuestion": None, "displayName": "Tech One", "comment": "", "email": "t@example.com",
            "roles": ["Technician"], "forcePasswordChange": False}
    with caplog.at_level(logging.INFO):
        response = client.post("/v1/users/save", json=body, headers=WRITE)
    assert response.json() == {"userName": "tech-one", "oldUserName": "tech1", "created": False}
    assert fake.calls_to("native", "SecurityService.ashx/SaveUser") == [
        ["InternalMembershipProvider", "tech1", "tech-one", None, None, None, "Tech One", "", "t@example.com",
         ["Technician"], False]
    ]
    audit = [json.loads(r.getMessage()) for r in caplog.records if r.name == "gateway.audit"]
    assert audit[-1]["endpoint"] == "POST /v1/users/save" and audit[-1]["status"] == 200


def test_16_save_user_update_requires_every_replaced_field(client, fake):
    body = {"oldUserName": "tech1", "newUserName": "tech1", "displayName": "Tech"}
    response = client.post("/v1/users/save", json=body, headers=WRITE)
    assert response.status_code == 400
    message = response.json()["message"]
    for field in ("comment", "email", "roles", "passwordQuestion", "forcePasswordChange"):
        assert field in message
    assert fake.calls == []


def test_16_save_user_validation_and_secrecy(make_client, fake, caplog):
    client = make_client(allowed_user_roles=("Technician",))
    mismatch = {"newUserName": "u", "password": "Secret-Pass-1", "verifyPassword": "Other-Pass-2"}
    response = client.post("/v1/users/save", json=mismatch, headers=WRITE)
    assert response.status_code == 400 and "Secret-Pass-1" not in response.text
    assert client.post("/v1/users/save", json={"newUserName": "u"}, headers=WRITE).status_code == 400  # create needs password
    admin = {"newUserName": "u", "password": "Secret-Pass-1", "roles": ["Administrator"]}
    with caplog.at_level(logging.DEBUG):
        assert client.post("/v1/users/save", json=admin, headers=WRITE).status_code == 403
    assert "Secret-Pass-1" not in caplog.text
    assert fake.calls_to("native", "SecurityService.ashx/SaveUser") == []


def test_16_user_management_is_disabled_by_default(make_client, fake):
    client = make_client(allowed_user_roles=())
    body = {"newUserName": "u", "password": "Secret-Pass-1"}
    response = client.post("/v1/users/save", json=body, headers=WRITE)
    assert response.status_code == 403
    assert response.json() == {"message": "user management is disabled; set ALLOWED_USER_ROLES to enable it"}
    assert client.post("/v1/users", json={"userName": "u", "password": "Secret-Pass-1"}, headers=WRITE).status_code == 403
    assert fake.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"newUserName": "SVC-GATEWAY", "password": "Secret-Pass-1"},
        {"oldUserName": "svc-gateway", "newUserName": "other", "password": None, "passwordQuestion": None,
         "displayName": "", "comment": "", "email": "", "roles": [], "forcePasswordChange": False},
    ],
)
def test_16_service_account_cannot_be_targeted(client, fake, body):
    """conftest's service account is "svc-gateway"."""
    response = client.post("/v1/users/save", json=body, headers=WRITE)
    assert response.status_code == 403
    assert "service account" in response.json()["message"]
    assert fake.calls == []


@pytest.mark.parametrize("name", [" svc-gateway", "svc-gateway\t", "svc-gateway\u200b", "x\u0000y"])
def test_16_padded_or_control_character_names_rejected(client, fake, name):
    response = client.post("/v1/users/save", json={"newUserName": name, "password": "Secret-Pass-1"}, headers=WRITE)
    assert response.status_code == 400
    assert fake.calls == []


def test_16_unicode_lookalike_of_service_account_blocked(client, fake):
    fullwidth = "ｓｖｃ－ｇａｔｅｗａｙ"  # NFKC-normalizes to svc-gateway
    response = client.post("/v1/users/save", json={"newUserName": fullwidth, "password": "Secret-Pass-1"}, headers=WRITE)
    assert response.status_code == 403 and "service account" in response.json()["message"]
    assert fake.calls == []


# ======================= backend consistency =======================


def test_native_name_search_without_type_covers_all_types(make_client, fake):
    client = make_client(backend_preference="native-first")
    typed_world(fake, [raw_session(name="Shared"), raw_session(SUPPORT_ID, name="Shared", session_type=0)])
    response = client.get("/v1/sessions", params={"name": "Shared"}, headers=READ)
    assert sorted(s["sessionType"] for s in response.json()) == [0, 2]
    assert response.headers["X-Gateway-Truncated"] == "false"
    assert [(r["sessionType"], r["sessionGroupPathParts"]) for r in host_info_requests(fake)] == [
        (2, ["All Machines"]), (0, ["All Sessions"]), (1, ["All Meetings"])
    ]


def test_filter_expressions_are_not_emulated_natively(client, fake):
    response = client.get("/v1/sessions", params={"filter": "SessionType = 'Access'", "backend": "native"}, headers=READ)
    assert response.status_code == 400
    assert fake.calls_to("native", "PageService.ashx/GetLiveData") == []


def test_raw_endpoints_are_read_only_and_allowlisted(client):
    assert client.post("/v1/raw/native/live-data", headers=WRITE).status_code == 405
    assert client.get("/v1/raw/native/live-data").status_code == 401
    spec = client.get("/openapi.json").json()
    raw_paths = [path for path in spec["paths"] if path.startswith("/v1/raw/")]
    assert len(raw_paths) == 5 and all(set(spec["paths"][p]) == {"get"} for p in raw_paths)
